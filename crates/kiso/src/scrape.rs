//! The metrics listener: a small [`Backend`] that answers one path with the
//! exposition text, and the snapshots the workers of one supervisor share.
//!
//! The listener is separate from the application's, so the application's host,
//! CORS and redirect rules, its request limits and its routes never apply to a
//! scrape, and the endpoint is not exposed by accident when the application is.
//! Every worker of a supervised run accepts on the same listener, so a scrape
//! reaches any of them. To answer for all, each worker writes a snapshot of its
//! own registry to a private directory every interval, and a scrape adds the
//! fresh snapshots of the others to its own live numbers. Each worker keeps its
//! own series (the `worker` label); counters are never summed across processes,
//! so a restarted worker restarts only its own series, which is a reset that
//! `rate()` handles.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use tokio::net::TcpListener;
use tokio::time::sleep;

use crate::accept::{serve, ServerConfig, Stop};
use crate::conn::{Answer, Backend, Incoming};
use crate::headers::Header;
use crate::metrics::{merge, Registry};
use crate::proxy::Networks;
use crate::stream::report;

/// The content type of the text exposition format, version 0.0.4.
const CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";
/// A snapshot larger than this is skipped by the reader and never written.
const MAX_SNAPSHOT: u64 = 4 * 1024 * 1024;
/// Most snapshot files one scrape reads.
const MAX_SNAPSHOTS: usize = 256;
/// A snapshot older than this many intervals belongs to a worker that is gone.
const FRESH_INTERVALS: u32 = 5;

/// Where the other workers' snapshots are, and how often they are written.
#[derive(Debug, Clone)]
pub struct Shared {
    pub dir: PathBuf,
    pub slot: u32,
    pub interval: Duration,
}

impl Shared {
    fn file(&self, slot: u32) -> PathBuf {
        self.dir.join(format!("worker-{slot}.prom"))
    }
}

/// Who may scrape what, and from where.
#[derive(Debug, Clone, Default)]
pub struct Access {
    pub path: String,
    /// Peers that may scrape; empty allows every peer.
    pub allow: Option<Networks>,
    /// The bearer token a scrape must send; `None` asks for none.
    pub token: Option<String>,
}

/// The backend of the metrics listener.
pub struct Endpoint {
    registry: Arc<Registry>,
    access: Access,
    shared: Option<Shared>,
}

impl Endpoint {
    pub fn new(registry: Arc<Registry>, access: Access, shared: Option<Shared>) -> Self {
        Self {
            registry,
            access,
            shared,
        }
    }

    /// The text of a scrape: this worker's live numbers and the other workers'
    /// fresh snapshots.
    pub async fn exposition(&self) -> String {
        let own = self.registry.render();
        let Some(shared) = &self.shared else {
            return own;
        };
        let peers = read_peers(shared, &self.registry).await;
        let mut parts: Vec<&str> = vec![&own];
        parts.extend(peers.iter().map(String::as_str));
        merge(&parts)
    }

    fn refusal(&self, request: &Incoming) -> Option<Answer> {
        if request.method != "GET" && request.method != "HEAD" {
            let mut answer = status_only(405);
            answer
                .headers
                .push((b"allow".to_vec(), b"GET, HEAD".to_vec()));
            return Some(answer);
        }
        if request.path != self.access.path {
            return Some(status_only(404));
        }
        if let Some(allow) = &self.access.allow {
            if !allow.contains(request.peer.ip()) {
                return Some(status_only(403));
            }
        }
        match &self.access.token {
            Some(token) if !bearer_matches(&request.headers, token) => {
                let mut answer = status_only(401);
                answer
                    .headers
                    .push((b"www-authenticate".to_vec(), b"Bearer".to_vec()));
                Some(answer)
            }
            _ => None,
        }
    }
}

fn status_only(status: u16) -> Answer {
    Answer {
        status,
        headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
        body: bytes::Bytes::new(),
    }
}

/// Whether the single `authorization` header is `Bearer <token>`, compared in
/// time that does not depend on where the tokens differ.
fn bearer_matches(headers: &[Header], token: &str) -> bool {
    let mut given = headers
        .iter()
        .filter(|(name, _)| name == b"authorization")
        .map(|(_, value)| value.as_slice());
    let (Some(value), None) = (given.next(), given.next()) else {
        return false;
    };
    let Some(presented) = value
        .strip_prefix(b"Bearer ")
        .or_else(|| value.strip_prefix(b"bearer "))
    else {
        return false;
    };
    let wanted = token.as_bytes();
    let mut difference = presented.len() ^ wanted.len();
    for index in 0..presented.len().max(wanted.len()) {
        let a = presented.get(index).copied().unwrap_or(0);
        let b = wanted.get(index).copied().unwrap_or(0);
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

impl Backend for Endpoint {
    async fn handle(&self, request: Incoming) -> Answer {
        if let Some(refusal) = self.refusal(&request) {
            return refusal;
        }
        let text = self.exposition().await.into_bytes();
        Answer {
            status: 200,
            headers: vec![
                (b"content-type".to_vec(), CONTENT_TYPE.as_bytes().to_vec()),
                (b"cache-control".to_vec(), b"no-store".to_vec()),
                (
                    b"content-length".to_vec(),
                    text.len().to_string().into_bytes(),
                ),
            ],
            body: if request.method == "HEAD" {
                bytes::Bytes::new()
            } else {
                bytes::Bytes::from(text)
            },
        }
    }
}

async fn read_peers(shared: &Shared, registry: &Registry) -> Vec<String> {
    let Ok(mut entries) = tokio::fs::read_dir(&shared.dir).await else {
        registry.snapshot_error();
        return Vec::new();
    };
    let own = shared.file(shared.slot);
    let window = shared.interval * FRESH_INTERVALS;
    let mut found = Vec::new();
    let mut seen = 0;
    while let Ok(Some(entry)) = entries.next_entry().await {
        let path = entry.path();
        let named = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("worker-") && name.ends_with(".prom"));
        if !named || path == own {
            continue;
        }
        seen += 1;
        if seen > MAX_SNAPSHOTS {
            registry.snapshot_error();
            break;
        }
        match read_snapshot(&path, window).await {
            Ok(Some(text)) => found.push(text),
            Ok(None) => {}
            Err(()) => registry.snapshot_error(),
        }
    }
    found.sort();
    found
}

/// The snapshot at `path`, `None` when it is older than `window` (a worker that
/// is gone), an error when it cannot be used.
async fn read_snapshot(path: &Path, window: Duration) -> Result<Option<String>, ()> {
    let meta = tokio::fs::metadata(path).await.map_err(|_| ())?;
    let age = meta
        .modified()
        .ok()
        .and_then(|time| SystemTime::now().duration_since(time).ok())
        .unwrap_or_default();
    if age > window {
        return Ok(None);
    }
    if meta.len() > MAX_SNAPSHOT {
        return Err(());
    }
    let bytes = tokio::fs::read(path).await.map_err(|_| ())?;
    String::from_utf8(bytes).map(Some).map_err(|_| ())
}

/// Write `body` to `target` through `temporary`, unless it is larger than a peer
/// would read: then `target` keeps its previous content.
async fn write_snapshot(body: &str, temporary: &Path, target: &Path) -> std::io::Result<()> {
    if body.len() as u64 > MAX_SNAPSHOT {
        return Err(std::io::Error::other(format!(
            "{} bytes is over the {MAX_SNAPSHOT} bytes the other workers read, so the previous snapshot is kept",
            body.len()
        )));
    }
    tokio::fs::write(temporary, body).await?;
    tokio::fs::rename(temporary, target).await
}

/// Write this worker's snapshot every interval until `stop`. The file is
/// written under a temporary name and renamed, so a scrape never reads half of it.
pub async fn publish(registry: Arc<Registry>, shared: Shared, mut stop: Stop) {
    let target = shared.file(shared.slot);
    let temporary = shared.dir.join(format!("worker-{}.tmp", shared.slot));
    let mut failing = false;
    loop {
        let written = write_snapshot(&registry.render(), &temporary, &target).await;
        match written {
            Err(error) if !failing => {
                failing = true;
                report(&format!(
                    "cannot write the metrics snapshot {}: {error}",
                    target.display()
                ));
            }
            Ok(()) => failing = false,
            Err(_) => {}
        }
        tokio::select! {
            () = sleep(shared.interval) => {}
            _ = stop.wait_for(|requested| *requested) => return,
        }
    }
}

/// Serve scrapes on `listener` until `stop`. The limits are small and fixed: a
/// scrape is a short request without a body.
pub async fn serve_metrics(listener: TcpListener, endpoint: Arc<Endpoint>, stop: Stop) {
    let config = ServerConfig {
        max_connections: 64,
        header_read_timeout: Duration::from_secs(5),
        body_read_timeout: Duration::from_secs(5),
        write_timeout: Duration::from_secs(10),
        shutdown_timeout: Duration::from_secs(1),
        max_header_bytes: 8 * 1024,
        max_headers: 50,
        ..ServerConfig::default()
    };
    serve(listener, endpoint, config, None, stop).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header(value: &str) -> Header {
        (b"authorization".to_vec(), value.as_bytes().to_vec())
    }

    #[tokio::test]
    async fn an_oversize_snapshot_is_refused_and_the_previous_file_kept() {
        let dir = std::env::temp_dir().join(format!("hinoki-scrape-unit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (temporary, target) = (dir.join("w.tmp"), dir.join("w.prom"));
        write_snapshot("small", &temporary, &target).await.unwrap();
        let big = "x".repeat(MAX_SNAPSHOT as usize + 1);
        let error = write_snapshot(&big, &temporary, &target).await.unwrap_err();
        assert!(error.to_string().contains("previous snapshot is kept"));
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "small");
        assert!(!temporary.exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_the_exact_bearer_token_matches() {
        assert!(bearer_matches(&[header("Bearer s3cret")], "s3cret"));
        assert!(!bearer_matches(&[header("Bearer s3cre")], "s3cret"));
        assert!(!bearer_matches(&[header("Bearer s3cretx")], "s3cret"));
        assert!(!bearer_matches(&[header("Basic s3cret")], "s3cret"));
        assert!(!bearer_matches(&[], "s3cret"));
        let twice = [header("Bearer s3cret"), header("Bearer s3cret")];
        assert!(!bearer_matches(&twice, "s3cret"));
    }
}
