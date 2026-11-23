//! The SMTP client for one connection: connect, greeting, `EHLO`, `STARTTLS`,
//! `AUTH` and one mail transaction. Every step has a timeout that is also capped
//! by the deadline of the whole send.

pub(crate) mod data;
pub(crate) mod reply;

use std::future::Future;
use std::net::{IpAddr, SocketAddr};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;
use rustls_pki_types::ServerName;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{timeout, timeout_at, Instant};
use tokio_rustls::TlsConnector;

use crate::config::{Credentials, Security, SmtpConfig};
use crate::error::{Kind, MailError};
use crate::message::Rendered;
use data::DotStuffer;
use reply::{read_reply, ReadError, Reply};

const QUIT_TIMEOUT: Duration = Duration::from_secs(1);
const CHUNK: usize = 16 * 1024;

pub(crate) trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

/// What an `EHLO` reply advertises, as far as this client uses it.
#[derive(Debug, Default, PartialEq, Eq)]
struct Capabilities {
    starttls: bool,
    plain: bool,
    login: bool,
    /// The server takes `SIZE=` on `MAIL FROM` (RFC 1870).
    size_param: bool,
    /// The largest message it declares; `SIZE`, `SIZE 0` and an unreadable
    /// value declare no limit.
    max_size: Option<usize>,
}

impl Capabilities {
    /// `lines` are the lines of the reply after the greeting line.
    fn parse(lines: &[String]) -> Self {
        let mut caps = Self::default();
        for line in lines {
            let upper = line.to_ascii_uppercase();
            let mut words = upper.split_ascii_whitespace();
            match words.next() {
                Some("STARTTLS") => caps.starttls = true,
                Some("SIZE") => {
                    caps.size_param = true;
                    caps.max_size = words
                        .next()
                        .and_then(|n| n.parse().ok())
                        .filter(|&max| max > 0);
                }
                Some(word) if word == "AUTH" || word.starts_with("AUTH=") => {
                    let first = word.strip_prefix("AUTH=");
                    for mechanism in first.into_iter().chain(words) {
                        caps.plain |= mechanism == "PLAIN";
                        caps.login |= mechanism == "LOGIN";
                    }
                }
                _ => {}
            }
        }
        caps
    }
}

/// Why a transaction failed, and what that says about the connection.
#[derive(Debug)]
pub(crate) struct Failure {
    pub(crate) error: MailError,
    /// At least one command of the transaction was answered well, so the
    /// connection was alive when it started.
    pub(crate) progressed: bool,
    /// The connection is idle again and may serve another message.
    pub(crate) reusable: bool,
}

/// Run `fut` until `end`; a late answer is a `Timeout`.
async fn within_until<T>(
    end: Instant,
    fut: impl Future<Output = Result<T, MailError>>,
) -> Result<T, MailError> {
    timeout_at(end, fut)
        .await
        .unwrap_or_else(|_| Err(MailError::new(Kind::Timeout)))
}

/// Run `fut` for at most `limit`, and never past `deadline`.
async fn within<T>(
    limit: Duration,
    deadline: Instant,
    fut: impl Future<Output = Result<T, MailError>>,
) -> Result<T, MailError> {
    within_until(deadline.min(Instant::now() + limit), fut).await
}

fn protocol() -> MailError {
    MailError::new(Kind::Protocol)
}

fn read_error(_: ReadError) -> MailError {
    protocol()
}

/// `reply` when it is the expected one (any 2xx for 250), otherwise the error
/// its class stands for.
fn expect_code(reply: Reply, want: u16) -> Result<Reply, MailError> {
    if reply.code == want || (want == 250 && reply.code / 100 == 2) {
        return Ok(reply);
    }
    let kind = match reply.code / 100 {
        4 => Kind::Temporary,
        5 => Kind::Rejected,
        _ => Kind::Protocol,
    };
    Err(MailError::with_reply(kind, reply.code, reply.enhanced))
}

/// A refusal during authentication is an `Auth` error; anything else keeps its
/// kind (a 4xx can be tried again).
fn auth_error(mut error: MailError) -> MailError {
    if error.kind == Kind::Rejected {
        error.kind = Kind::Auth;
    }
    error
}

fn tls_error(mut error: MailError) -> MailError {
    if error.kind == Kind::Rejected {
        error.kind = Kind::Tls;
    }
    error
}

/// The `EHLO` argument: the configured name or the local address as a literal.
fn helo_name(config: &SmtpConfig, local: IpAddr) -> String {
    match (&config.helo_name, local) {
        (Some(name), _) => name.clone(),
        (None, IpAddr::V4(ip)) => format!("[{ip}]"),
        (None, IpAddr::V6(ip)) => format!("[IPv6:{ip}]"),
    }
}

async fn handshake(
    tls: &TlsConnector,
    host: &str,
    io: Box<dyn Io>,
    end: Instant,
) -> Result<Box<dyn Io>, MailError> {
    let tls_failed = || MailError::new(Kind::Tls);
    let name = ServerName::try_from(host.to_owned()).map_err(|_| tls_failed())?;
    let stream = within_until(end, async {
        tls.connect(name, io).await.map_err(|_| tls_failed())
    })
    .await?;
    Ok(Box::new(stream))
}

async fn tcp_connect(config: &SmtpConfig, end: Instant) -> Result<TcpStream, MailError> {
    let stream = within_until(end, async {
        TcpStream::connect((config.server_host(), config.port))
            .await
            .map_err(|_| MailError::new(Kind::Connect))
    })
    .await?;
    // Mail is sent in a few small writes; do not let them wait for each other.
    stream
        .set_nodelay(true)
        .map_err(|_| MailError::new(Kind::Connect))?;
    Ok(stream)
}

pub(crate) struct Connection {
    io: Box<dyn Io>,
    pending: Vec<u8>,
    caps: Capabilities,
    secure: bool,
    command_timeout: Duration,
    data_timeout: Duration,
    /// Commands of the current transaction that were answered as expected.
    progress: u32,
    /// The end marker of the message was written.
    terminator_written: bool,
}

/// Connect to the server of `config` and get to the point where messages can be
/// sent: TLS, `EHLO` and `AUTH` done.
pub(crate) async fn open(
    config: &SmtpConfig,
    tls: &TlsConnector,
    deadline: Instant,
) -> Result<Connection, MailError> {
    let end = deadline.min(Instant::now() + config.connect_timeout);
    let stream = tcp_connect(config, end).await?;
    let (local, peer) = addresses(&stream)?;
    if config.security == Security::None && !config.allow_insecure && !peer.ip().is_loopback() {
        return Err(MailError::new(Kind::Tls));
    }
    let mut io: Box<dyn Io> = Box::new(stream);
    if config.security == Security::Tls {
        io = handshake(tls, config.server_host(), io, end).await?;
    }
    let mut conn = Connection::new(io, config, config.security == Security::Tls);
    conn.expect_greeting(deadline).await?;
    let helo = helo_name(config, local.ip());
    conn.ehlo(&helo, deadline).await?;
    if config.security == Security::StartTls {
        conn = conn.start_tls(config, tls, deadline).await?;
        conn.ehlo(&helo, deadline).await?;
    }
    if let Some(credentials) = &config.credentials {
        let trusted = conn.secure || config.allow_insecure || peer.ip().is_loopback();
        conn.authenticate(credentials, trusted, deadline).await?;
    }
    Ok(conn)
}

fn addresses(stream: &TcpStream) -> Result<(SocketAddr, SocketAddr), MailError> {
    let connect = || MailError::new(Kind::Connect);
    Ok((
        stream.local_addr().map_err(|_| connect())?,
        stream.peer_addr().map_err(|_| connect())?,
    ))
}

impl Connection {
    fn new(io: Box<dyn Io>, config: &SmtpConfig, secure: bool) -> Self {
        Self {
            io,
            pending: Vec::new(),
            caps: Capabilities::default(),
            secure,
            command_timeout: config.command_timeout,
            data_timeout: config.data_timeout,
            progress: 0,
            terminator_written: false,
        }
    }

    async fn read(&mut self, limit: Duration, deadline: Instant) -> Result<Reply, MailError> {
        within(limit, deadline, async {
            read_reply(&mut self.io, &mut self.pending)
                .await
                .map_err(read_error)
        })
        .await
    }

    /// Send `line` (a CRLF is added) and read the reply. The bytes of `line`
    /// are overwritten afterwards, as they may hold a credential.
    async fn command(&mut self, mut line: Vec<u8>, deadline: Instant) -> Result<Reply, MailError> {
        line.extend_from_slice(b"\r\n");
        let limit = self.command_timeout;
        let written = within(limit, deadline, async {
            self.io.write_all(&line).await.map_err(|_| protocol())?;
            self.io.flush().await.map_err(|_| protocol())
        })
        .await;
        line.fill(0);
        written?;
        self.read(limit, deadline).await
    }

    async fn expect(
        &mut self,
        line: &str,
        want: u16,
        deadline: Instant,
    ) -> Result<Reply, MailError> {
        let reply = self.command(line.as_bytes().to_vec(), deadline).await?;
        self.accepted(reply, want)
    }

    fn accepted(&mut self, reply: Reply, want: u16) -> Result<Reply, MailError> {
        let reply = expect_code(reply, want)?;
        self.progress += 1;
        Ok(reply)
    }

    async fn expect_greeting(&mut self, deadline: Instant) -> Result<(), MailError> {
        let reply = self.read(self.command_timeout, deadline).await?;
        expect_code(reply, 220).map(|_| ())
    }

    async fn ehlo(&mut self, name: &str, deadline: Instant) -> Result<(), MailError> {
        let reply = self.expect(&format!("EHLO {name}"), 250, deadline).await?;
        self.caps = Capabilities::parse(reply.lines.get(1..).unwrap_or_default());
        Ok(())
    }

    /// Upgrade this connection with `STARTTLS`; the server must offer it.
    async fn start_tls(
        mut self,
        config: &SmtpConfig,
        tls: &TlsConnector,
        deadline: Instant,
    ) -> Result<Connection, MailError> {
        if !self.caps.starttls {
            return Err(MailError::new(Kind::Tls));
        }
        self.expect("STARTTLS", 220, deadline)
            .await
            .map_err(tls_error)?;
        // Bytes the server sent before the handshake are not part of the TLS
        // session, and must not be read as if they were.
        if !self.pending.is_empty() {
            return Err(MailError::new(Kind::Tls));
        }
        let end = deadline.min(Instant::now() + config.connect_timeout);
        let io = handshake(tls, config.server_host(), self.io, end).await?;
        Ok(Connection::new(io, config, true))
    }

    async fn authenticate(
        &mut self,
        credentials: &Credentials,
        trusted: bool,
        deadline: Instant,
    ) -> Result<(), MailError> {
        let auth = MailError::new(Kind::Auth);
        if !trusted {
            return Err(auth);
        }
        let result = if self.caps.plain {
            self.auth_plain(credentials, deadline).await
        } else if self.caps.login {
            self.auth_login(credentials, deadline).await
        } else {
            return Err(auth);
        };
        result.map_err(auth_error)
    }

    async fn auth_plain(
        &mut self,
        credentials: &Credentials,
        deadline: Instant,
    ) -> Result<(), MailError> {
        let mut raw = vec![0u8];
        raw.extend_from_slice(credentials.username.as_bytes());
        raw.push(0);
        raw.extend_from_slice(credentials.password.as_bytes());
        let mut line = b"AUTH PLAIN ".to_vec();
        line.extend_from_slice(STANDARD.encode(&raw).as_bytes());
        raw.fill(0);
        let reply = self.command(line, deadline).await?;
        self.accepted(reply, 235).map(|_| ())
    }

    async fn auth_login(
        &mut self,
        credentials: &Credentials,
        deadline: Instant,
    ) -> Result<(), MailError> {
        self.expect("AUTH LOGIN", 334, deadline).await?;
        let user = STANDARD
            .encode(credentials.username.as_bytes())
            .into_bytes();
        let reply = self.command(user, deadline).await?;
        self.accepted(reply, 334)?;
        let pass = STANDARD
            .encode(credentials.password.as_bytes())
            .into_bytes();
        let reply = self.command(pass, deadline).await?;
        self.accepted(reply, 235).map(|_| ())
    }

    /// Whether any command of the last [`Connection::send`] was answered well.
    pub(crate) fn progressed(&self) -> bool {
        self.progress > 0
    }

    /// Send one message. On failure the [`Failure`] says whether the connection
    /// can be used again.
    pub(crate) async fn send(
        &mut self,
        message: &Rendered,
        deadline: Instant,
    ) -> Result<(), Failure> {
        self.progress = 0;
        self.terminator_written = false;
        if self
            .caps
            .max_size
            .is_some_and(|max| message.bytes.len() > max)
        {
            return Err(Failure {
                error: MailError::new(Kind::LimitExceeded),
                progressed: false,
                reusable: true,
            });
        }
        match self.transaction(message, deadline).await {
            Ok(()) => Ok(()),
            Err(error) => Err(self.failure(error, deadline).await),
        }
    }

    async fn failure(&mut self, error: MailError, deadline: Instant) -> Failure {
        // A definite answer from the server leaves it waiting for the next
        // transaction; a reset makes sure. A closing server (421) does not.
        let answered =
            matches!(error.kind, Kind::Temporary | Kind::Rejected) && error.code != Some(421);
        let progressed = self.progressed();
        let reusable = answered && self.reset(deadline).await;
        Failure {
            error,
            progressed,
            reusable,
        }
    }

    async fn reset(&mut self, deadline: Instant) -> bool {
        self.expect("RSET", 250, deadline).await.is_ok()
    }

    async fn transaction(
        &mut self,
        message: &Rendered,
        deadline: Instant,
    ) -> Result<(), MailError> {
        let envelope = &message.envelope;
        let size = if self.caps.size_param {
            format!(" SIZE={}", message.bytes.len())
        } else {
            String::new()
        };
        self.expect(
            &format!("MAIL FROM:<{}>{size}", envelope.from.as_str()),
            250,
            deadline,
        )
        .await?;
        for (position, to) in envelope.to.iter().enumerate() {
            self.expect(&format!("RCPT TO:<{}>", to.as_str()), 250, deadline)
                .await
                .map_err(|mut error| {
                    if let Some((field, index)) = envelope.origins.get(position) {
                        error.field = Some(*field);
                        error.index = u32::try_from(*index).ok();
                    }
                    error
                })?;
        }
        self.expect("DATA", 354, deadline).await?;
        self.send_data(&message.bytes, deadline).await
    }

    async fn send_data(&mut self, bytes: &[u8], deadline: Instant) -> Result<(), MailError> {
        let limit = self.data_timeout;
        let result = within(limit, deadline, async {
            let reply = self.write_payload(bytes).await?;
            expect_code(reply, 250)
        })
        .await;
        match result {
            Ok(_) => Ok(()),
            Err(mut error) => {
                // Without a definite answer after the end marker, the server may
                // have accepted the message.
                error.possibly_sent =
                    self.terminator_written && matches!(error.kind, Kind::Timeout | Kind::Protocol);
                Err(error)
            }
        }
    }

    async fn write_payload(&mut self, bytes: &[u8]) -> Result<Reply, MailError> {
        let mut stuffer = DotStuffer::new();
        let mut out = Vec::with_capacity(CHUNK + CHUNK / 8);
        for chunk in bytes.chunks(CHUNK) {
            out.clear();
            stuffer.push(chunk, &mut out);
            self.io.write_all(&out).await.map_err(|_| protocol())?;
        }
        out.clear();
        stuffer.finish(&mut out);
        self.terminator_written = true;
        self.io.write_all(&out).await.map_err(|_| protocol())?;
        self.io.flush().await.map_err(|_| protocol())?;
        read_reply(&mut self.io, &mut self.pending)
            .await
            .map_err(read_error)
    }

    /// Say goodbye, for at most a second; errors do not matter.
    pub(crate) async fn quit(&mut self) {
        let _ = timeout(QUIT_TIMEOUT, async {
            self.io.write_all(b"QUIT\r\n").await.ok()?;
            self.io.flush().await.ok()?;
            read_reply(&mut self.io, &mut self.pending).await.ok()
        })
        .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(text: &[&str]) -> Vec<String> {
        text.iter().map(|line| (*line).to_owned()).collect()
    }

    #[test]
    fn capabilities_are_read_case_insensitively() {
        let caps = Capabilities::parse(&lines(&[
            "starttls",
            "SIZE 5000",
            "AUTH PLAIN LOGIN XOAUTH2",
            "8BITMIME",
        ]));
        assert_eq!(
            caps,
            Capabilities {
                starttls: true,
                plain: true,
                login: true,
                size_param: true,
                max_size: Some(5000)
            }
        );
    }

    #[test]
    fn the_legacy_auth_form_and_a_size_without_a_limit_are_understood() {
        let caps = Capabilities::parse(&lines(&["AUTH=LOGIN", "AUTH LOGIN", "SIZE", "SIZE abc"]));
        assert!(caps.login && !caps.plain && !caps.starttls);
        assert!(caps.size_param);
        assert_eq!(caps.max_size, None);
        let zero = Capabilities::parse(&lines(&["SIZE 0"]));
        assert!(zero.size_param);
        assert_eq!(zero.max_size, None);
        assert_eq!(Capabilities::parse(&[]), Capabilities::default());
        let caps = Capabilities::parse(&lines(&["AUTH=PLAIN LOGIN"]));
        assert!(caps.plain && caps.login);
    }

    #[test]
    fn replies_map_to_error_kinds_by_class() {
        let reply = |code| Reply {
            code,
            enhanced: Some("5.1.1".to_owned()),
            lines: Vec::new(),
        };
        assert!(expect_code(reply(250), 250).is_ok());
        assert!(expect_code(reply(251), 250).is_ok());
        assert!(expect_code(reply(354), 354).is_ok());
        assert_eq!(
            expect_code(reply(250), 354).unwrap_err().kind,
            Kind::Protocol
        );
        assert_eq!(
            expect_code(reply(451), 250).unwrap_err().kind,
            Kind::Temporary
        );
        let rejected = expect_code(reply(550), 250).unwrap_err();
        assert_eq!(rejected.kind, Kind::Rejected);
        assert_eq!(rejected.code, Some(550));
        assert_eq!(rejected.enhanced.as_deref(), Some("5.1.1"));
        assert_eq!(
            expect_code(reply(334), 235).unwrap_err().kind,
            Kind::Protocol
        );
    }

    #[test]
    fn auth_and_tls_refusals_change_the_kind_only_for_5xx() {
        assert_eq!(auth_error(MailError::new(Kind::Rejected)).kind, Kind::Auth);
        assert_eq!(
            auth_error(MailError::new(Kind::Temporary)).kind,
            Kind::Temporary
        );
        assert_eq!(tls_error(MailError::new(Kind::Rejected)).kind, Kind::Tls);
        assert_eq!(tls_error(MailError::new(Kind::Timeout)).kind, Kind::Timeout);
    }
}
