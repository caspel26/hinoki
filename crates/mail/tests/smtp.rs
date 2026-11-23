//! The SMTP client against a scripted server on the loopback interface.

use std::collections::HashMap;
use std::future::Future;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use hinoki_mail::{
    mailboxes, Backend, Config, Credentials, Draft, Field, Kind, Limits, MailError, Mailer,
    Observer, PoolConfig, RetryPolicy, Security, Sent, SmtpConfig,
};
use rustls::crypto::ring::default_provider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::ServerConfig;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio_rustls::TlsAcceptor;

trait Stream: AsyncRead + AsyncWrite + Unpin + Send + Sync {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send + Sync> Stream for T {}

/// What the server does at a step instead of the normal thing.
#[derive(Clone)]
enum Hook {
    Normal,
    Reply(String),
    /// Reply, then close the connection.
    ReplyClose(String),
    Stall,
    Delay(u64),
}

type Hooks = Arc<dyn Fn(usize, &str, usize) -> Hook + Send + Sync>;

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Plain,
    Upgrade,
    Implicit,
}

struct Options {
    tls: Mode,
    advertise_starttls: bool,
    auth: Option<&'static str>,
    login: &'static str,
    password: &'static str,
    size: Option<usize>,
    hook: Hooks,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            tls: Mode::Plain,
            advertise_starttls: true,
            auth: None,
            login: "user",
            password: "secret",
            size: None,
            hook: Arc::new(|_, _, _| Hook::Normal),
        }
    }
}

#[derive(Default)]
struct State {
    connections: AtomicUsize,
    active: AtomicUsize,
    peak: AtomicUsize,
    counts: Mutex<HashMap<String, usize>>,
    commands: Mutex<Vec<String>>,
    /// Messages as the server decoded them, and as they came on the wire.
    messages: Mutex<Vec<Vec<u8>>>,
    wire: Mutex<Vec<Vec<u8>>>,
}

impl State {
    fn count(&self, verb: &str) -> usize {
        *self.counts.lock().unwrap().get(verb).unwrap_or(&0)
    }

    fn saw(&self, prefix: &str) -> bool {
        self.commands
            .lock()
            .unwrap()
            .iter()
            .any(|c| c.starts_with(prefix))
    }
}

struct Server {
    addr: SocketAddr,
    state: Arc<State>,
    task: JoinHandle<()>,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/tls")
        .join(name)
}

fn acceptor() -> TlsAcceptor {
    let certs = CertificateDer::pem_file_iter(fixture("localhost-cert.pem"))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    let key = PrivateKeyDer::from_pem_file(fixture("localhost-key.pem")).unwrap();
    let config = ServerConfig::builder_with_provider(Arc::new(default_provider()))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .unwrap();
    TlsAcceptor::from(Arc::new(config))
}

async fn start(options: Options) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let state = Arc::new(State::default());
    let options = Arc::new(options);
    let tls = acceptor();
    let shared = Arc::clone(&state);
    let task = tokio::spawn(async move {
        loop {
            let Ok((socket, _)) = listener.accept().await else {
                return;
            };
            let index = shared.connections.fetch_add(1, Ordering::SeqCst);
            let (state, options, tls) = (Arc::clone(&shared), Arc::clone(&options), tls.clone());
            tokio::spawn(async move {
                let active = state.active.fetch_add(1, Ordering::SeqCst) + 1;
                state.peak.fetch_max(active, Ordering::SeqCst);
                let io: Box<dyn Stream> = if options.tls == Mode::Implicit {
                    match tls.accept(socket).await {
                        Ok(stream) => Box::new(stream),
                        Err(_) => return,
                    }
                } else {
                    Box::new(socket)
                };
                let mut session = Session {
                    io: BufReader::new(io),
                    state: Arc::clone(&state),
                    options,
                    tls,
                    index,
                    secure: false,
                    last: String::new(),
                };
                session.run().await;
                state.active.fetch_sub(1, Ordering::SeqCst);
            });
        }
    });
    Server { addr, state, task }
}

struct Session {
    io: BufReader<Box<dyn Stream>>,
    state: Arc<State>,
    options: Arc<Options>,
    tls: TlsAcceptor,
    index: usize,
    secure: bool,
    /// The code of the last reply sent.
    last: String,
}

impl Session {
    fn hook(&self, verb: &str) -> Hook {
        let nth = {
            let mut counts = self.state.counts.lock().unwrap();
            let slot = counts.entry(verb.to_owned()).or_insert(0);
            *slot += 1;
            *slot - 1
        };
        (self.options.hook)(self.index, verb, nth)
    }

    async fn say(&mut self, text: &str) -> bool {
        self.last = text.chars().take(3).collect();
        let io = self.io.get_mut();
        io.write_all(text.as_bytes()).await.is_ok() && io.flush().await.is_ok()
    }

    async fn line(&mut self) -> Option<String> {
        let mut line = String::new();
        match self.io.read_line(&mut line).await {
            Ok(0) | Err(_) => None,
            Ok(_) => Some(line.trim_end().to_owned()),
        }
    }

    /// Apply the hook of `verb`; `None` when the normal reply should be sent,
    /// otherwise whether to keep going.
    async fn apply(&mut self, verb: &str) -> Option<bool> {
        match self.hook(verb) {
            Hook::Normal => None,
            Hook::Reply(text) => Some(self.say(&text).await),
            Hook::ReplyClose(text) => {
                self.say(&text).await;
                Some(false)
            }
            Hook::Stall => {
                tokio::time::sleep(Duration::from_secs(3600)).await;
                Some(false)
            }
            Hook::Delay(ms) => {
                tokio::time::sleep(Duration::from_millis(ms)).await;
                None
            }
        }
    }

    async fn reply(&mut self, verb: &str, normal: &str) -> bool {
        match self.apply(verb).await {
            Some(keep) => keep,
            None => self.say(normal).await,
        }
    }

    async fn run(&mut self) {
        // A greeting that is not 220 ends the conversation.
        if !self.reply("CONNECT", "220 fake ESMTP\r\n").await || self.last != "220" {
            return;
        }
        while let Some(line) = self.line().await {
            self.state.commands.lock().unwrap().push(line.clone());
            if !self.command(&line).await {
                return;
            }
        }
    }

    async fn command(&mut self, line: &str) -> bool {
        let verb = line
            .split([' ', ':'])
            .next()
            .unwrap_or("")
            .to_ascii_uppercase();
        match verb.as_str() {
            "EHLO" => self.ehlo().await,
            "STARTTLS" => self.starttls().await,
            "AUTH" => self.auth(line).await,
            "MAIL" => self.reply("MAIL", "250 ok\r\n").await,
            "RCPT" => self.reply("RCPT", "250 ok\r\n").await,
            "RSET" => self.reply("RSET", "250 ok\r\n").await,
            "DATA" => self.data().await,
            "QUIT" => {
                self.say("221 bye\r\n").await;
                false
            }
            _ => self.say("500 what\r\n").await,
        }
    }

    async fn ehlo(&mut self) -> bool {
        let secure = self.secure || self.options.tls == Mode::Implicit;
        let mut lines = vec!["fake".to_owned()];
        if let Some(size) = self.options.size {
            lines.push(format!("SIZE {size}"));
        }
        if self.options.tls == Mode::Upgrade && !secure && self.options.advertise_starttls {
            lines.push("STARTTLS".to_owned());
        }
        if let Some(mechanisms) = self.options.auth {
            lines.push(format!("AUTH {mechanisms}"));
        }
        let last = lines.len() - 1;
        let mut text = String::new();
        for (index, line) in lines.iter().enumerate() {
            let sep = if index == last { ' ' } else { '-' };
            text.push_str(&format!("250{sep}{line}\r\n"));
        }
        self.reply("EHLO", &text).await
    }

    async fn starttls(&mut self) -> bool {
        if !self.reply("STARTTLS", "220 go ahead\r\n").await || self.last != "220" {
            return false;
        }
        let placeholder: Box<dyn Stream> = Box::new(tokio::io::duplex(1).0);
        let plain = std::mem::replace(self.io.get_mut(), placeholder);
        match self.tls.accept(plain).await {
            Ok(stream) => {
                self.io = BufReader::new(Box::new(stream));
                self.secure = true;
                true
            }
            Err(_) => false,
        }
    }

    fn credentials_ok(&self, user: &[u8], password: &[u8]) -> bool {
        user == self.options.login.as_bytes() && password == self.options.password.as_bytes()
    }

    async fn decode_line(&mut self) -> Option<Vec<u8>> {
        let line = self.line().await?;
        STANDARD.decode(line).ok()
    }

    async fn auth(&mut self, line: &str) -> bool {
        let ok = if let Some(token) = line.strip_prefix("AUTH PLAIN ") {
            let raw = STANDARD.decode(token).unwrap_or_default();
            let mut parts = raw.split(|b| *b == 0);
            let (_, user, password) = (parts.next(), parts.next(), parts.next());
            self.credentials_ok(user.unwrap_or_default(), password.unwrap_or_default())
        } else {
            if !self.say("334 VXNlcm5hbWU6\r\n").await {
                return false;
            }
            let Some(user) = self.decode_line().await else {
                return false;
            };
            if !self.say("334 UGFzc3dvcmQ6\r\n").await {
                return false;
            }
            let Some(password) = self.decode_line().await else {
                return false;
            };
            self.credentials_ok(&user, &password)
        };
        let normal = if ok {
            "235 2.7.0 welcome\r\n"
        } else {
            "535 5.7.8 no\r\n"
        };
        self.reply("AUTH", normal).await
    }

    async fn data(&mut self) -> bool {
        if !self.reply("DATA", "354 go\r\n").await {
            return false;
        }
        // A refusal of DATA (4xx or 5xx) leaves the server in command mode.
        if self.last != "354" {
            return true;
        }
        let (mut wire, mut message) = (Vec::new(), Vec::new());
        loop {
            let Some(line) = self.line().await else {
                return false;
            };
            wire.extend_from_slice(format!("{line}\r\n").as_bytes());
            if line == "." {
                break;
            }
            let line = line.strip_prefix('.').unwrap_or(&line);
            message.extend_from_slice(format!("{line}\r\n").as_bytes());
        }
        self.state.messages.lock().unwrap().push(message);
        self.state.wire.lock().unwrap().push(wire);
        self.reply("END", "250 2.0.0 queued\r\n").await
    }
}

fn draft() -> Draft {
    let one = |field, address: &str| mailboxes(field, vec![(address.to_owned(), None)]).unwrap();
    Draft {
        sender: one(Field::Sender, "from@example.com").remove(0),
        to: one(Field::To, "to@example.com"),
        cc: vec![],
        bcc: vec![],
        reply_to: vec![],
        subject: "Hello".to_owned(),
        text: Some("Body".to_owned()),
        html: None,
        attachments: vec![],
        headers: vec![],
    }
}

fn smtp(server: &Server, security: Security) -> SmtpConfig {
    SmtpConfig {
        host: "127.0.0.1".to_owned(),
        port: server.addr.port(),
        security,
        credentials: None,
        allow_insecure: false,
        tls_ca_file: Some(fixture("localhost-cert.pem")),
        helo_name: Some("client.example.com".to_owned()),
        connect_timeout: Duration::from_secs(2),
        command_timeout: Duration::from_millis(400),
        data_timeout: Duration::from_millis(400),
        pool: PoolConfig {
            max_connections: 2,
            wait: Duration::from_millis(500),
            idle_timeout: Duration::from_secs(60),
            max_messages_per_connection: 100,
        },
        retry: RetryPolicy {
            retries: 2,
            base: Duration::from_millis(5),
            max: Duration::from_millis(10),
        },
    }
}

fn mailer_of(smtp: SmtpConfig) -> Mailer {
    Mailer::new(Config {
        backend: Backend::Smtp(Box::new(smtp)),
        limits: Limits::default(),
        message_id_domain: "mail.example.com".to_owned(),
        timeout: Duration::from_secs(5),
    })
    .unwrap()
}

async fn mailer(server: &Server, security: Security) -> Mailer {
    mailer_of(smtp(server, security))
}

fn login() -> Option<Credentials> {
    Some(Credentials {
        username: "user".to_owned(),
        password: "secret".to_owned(),
    })
}

fn hook(f: impl Fn(usize, &str, usize) -> Hook + Send + Sync + 'static) -> Options {
    Options {
        hook: Arc::new(f),
        ..Options::default()
    }
}

async fn send(mailer: &Mailer) -> Result<Sent, MailError> {
    mailer.send(draft()).await
}

async fn eventually<F: Future<Output = bool>>(mut check: impl FnMut() -> F) {
    for _ in 0..100 {
        if check().await {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("condition not reached");
}

#[tokio::test]
async fn a_message_is_delivered_over_a_plain_connection() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    let sent = send(&mailer).await.unwrap();
    assert_eq!(sent.attempts, 1);
    assert_eq!(sent.recipients, ["to@example.com"]);
    assert!(sent.raw.is_none());
    let messages = server.state.messages.lock().unwrap();
    let body = String::from_utf8(messages[0].clone()).unwrap();
    assert!(body.contains("Subject: Hello\r\n"));
    assert!(body.contains(&sent.message_id));
    assert!(server.state.saw("EHLO client.example.com"));
    assert!(server.state.saw("MAIL FROM:<from@example.com>"));
    assert!(server.state.saw("RCPT TO:<to@example.com>"));
}

#[tokio::test]
async fn bcc_recipients_are_in_the_envelope_only() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    let mut d = draft();
    d.bcc = mailboxes(Field::Bcc, vec![("hidden@example.com".to_owned(), None)]).unwrap();
    mailer.send(d).await.unwrap();
    assert!(server.state.saw("RCPT TO:<hidden@example.com>"));
    let body = server.state.messages.lock().unwrap()[0].clone();
    assert!(!String::from_utf8(body)
        .unwrap()
        .contains("hidden@example.com"));
}

#[tokio::test]
async fn leading_dots_are_stuffed_on_the_wire_and_restored_by_the_server() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    let mut d = draft();
    d.text = Some("one\n.hidden\n..two\n.".to_owned());
    send_draft(&mailer, d).await;
    let wire = String::from_utf8(server.state.wire.lock().unwrap()[0].clone()).unwrap();
    let message = String::from_utf8(server.state.messages.lock().unwrap()[0].clone()).unwrap();
    assert!(wire.contains("\r\n..hidden\r\n") && wire.contains("\r\n...two\r\n"));
    assert!(message.contains("\r\n.hidden\r\n") && message.contains("\r\n..two\r\n"));
    // The server saw exactly one message, ended by the one terminator.
    assert!(wire.ends_with("\r\n.\r\n"));
    assert_eq!(wire.matches("\r\n.\r\n").count(), 1);
}

async fn send_draft(mailer: &Mailer, d: Draft) -> Sent {
    mailer.send(d).await.unwrap()
}

#[tokio::test]
async fn starttls_upgrades_before_authentication() {
    let server = start(Options {
        tls: Mode::Upgrade,
        auth: Some("PLAIN LOGIN"),
        ..Options::default()
    })
    .await;
    let mut config = smtp(&server, Security::StartTls);
    config.credentials = login();
    let mailer = mailer_of(config);
    send(&mailer).await.unwrap();
    let commands = server.state.commands.lock().unwrap().clone();
    let position = |prefix: &str| commands.iter().position(|c| c.starts_with(prefix)).unwrap();
    assert!(position("STARTTLS") < position("AUTH"));
    assert_eq!(commands.iter().filter(|c| c.starts_with("EHLO")).count(), 2);
}

#[tokio::test]
async fn implicit_tls_delivers() {
    let server = start(Options {
        tls: Mode::Implicit,
        ..Options::default()
    })
    .await;
    let mailer = mailer(&server, Security::Tls).await;
    assert_eq!(send(&mailer).await.unwrap().attempts, 1);
}

#[tokio::test]
async fn an_untrusted_certificate_is_refused_and_not_retried() {
    for (tls, security) in [
        (Mode::Implicit, Security::Tls),
        (Mode::Upgrade, Security::StartTls),
    ] {
        let server = start(Options {
            tls,
            ..Options::default()
        })
        .await;
        let mut config = smtp(&server, security);
        config.tls_ca_file = None;
        let mailer = mailer_of(config);
        let error = send(&mailer).await.unwrap_err();
        assert_eq!((error.kind, error.attempts), (Kind::Tls, 1));
        assert!(!server.state.saw("MAIL"));
    }
}

#[tokio::test]
async fn starttls_that_is_not_offered_is_an_error_not_a_downgrade() {
    let server = start(Options {
        tls: Mode::Upgrade,
        advertise_starttls: false,
        ..Options::default()
    })
    .await;
    let mailer = mailer(&server, Security::StartTls).await;
    let error = send(&mailer).await.unwrap_err();
    assert_eq!(error.kind, Kind::Tls);
    assert!(!server.state.saw("MAIL"));
}

#[tokio::test]
async fn a_refused_starttls_is_a_tls_error() {
    let server = start(Options {
        tls: Mode::Upgrade,
        hook: Arc::new(|_, verb, _| {
            if verb == "STARTTLS" {
                Hook::Reply("502 5.5.1 no tls\r\n".to_owned())
            } else {
                Hook::Normal
            }
        }),
        ..Options::default()
    })
    .await;
    let mailer = mailer(&server, Security::StartTls).await;
    let error = send(&mailer).await.unwrap_err();
    assert_eq!(error.kind, Kind::Tls);
}

#[tokio::test]
async fn auth_plain_and_the_login_fallback_succeed() {
    for mechanisms in ["PLAIN LOGIN", "LOGIN"] {
        let server = start(Options {
            auth: Some(mechanisms),
            ..Options::default()
        })
        .await;
        let mut config = smtp(&server, Security::None);
        config.credentials = login();
        let mailer = mailer_of(config);
        send(&mailer).await.unwrap();
        let plain = mechanisms.starts_with("PLAIN");
        assert_eq!(server.state.saw("AUTH PLAIN"), plain);
        assert_eq!(server.state.saw("AUTH LOGIN"), !plain);
    }
}

#[tokio::test]
async fn bad_credentials_are_an_auth_error_and_not_retried() {
    for mechanisms in ["PLAIN", "LOGIN"] {
        let server = start(Options {
            auth: Some(mechanisms),
            password: "other",
            ..Options::default()
        })
        .await;
        let mut config = smtp(&server, Security::None);
        config.credentials = login();
        let mailer = mailer_of(config);
        let error = send(&mailer).await.unwrap_err();
        assert_eq!(
            (error.kind, error.attempts, error.code),
            (Kind::Auth, 1, Some(535))
        );
        assert_eq!(error.enhanced.as_deref(), Some("5.7.8"));
        assert_eq!(server.state.connections.load(Ordering::SeqCst), 1);
        assert!(!format!("{error} {error:?}").contains("secret"));
    }
}

#[tokio::test]
async fn credentials_without_an_advertised_mechanism_fail() {
    let server = start(Options::default()).await;
    let mut config = smtp(&server, Security::None);
    config.credentials = login();
    let error = send(&mailer_of(config)).await.unwrap_err();
    assert_eq!(error.kind, Kind::Auth);
}

#[tokio::test]
async fn a_busy_server_at_connect_is_retried_then_succeeds() {
    let server = start(hook(|conn, verb, _| match (conn, verb) {
        (0, "CONNECT") => Hook::Reply("421 4.3.2 busy\r\n".to_owned()),
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let seen = Arc::new(Counts::default());
    assert!(mailer.attach(seen.clone()));
    assert!(!mailer.attach(seen.clone()));
    let sent = send(&mailer).await.unwrap();
    assert_eq!(sent.attempts, 2);
    assert_eq!(mailer.stats().retries, 1);
    assert_eq!(seen.retried.load(Ordering::SeqCst), 1);
    assert_eq!(seen.sent.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_temporary_refusal_of_data_is_retried_on_the_same_connection() {
    let server = start(hook(|_, verb, nth| match (verb, nth) {
        ("DATA", 0) => Hook::Reply("451 4.3.0 later\r\n".to_owned()),
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let sent = send(&mailer).await.unwrap();
    assert_eq!(sent.attempts, 2);
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 1);
    assert!(server.state.saw("RSET"));
    assert_eq!(server.state.messages.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_permanent_refusal_is_final_names_the_recipient_and_keeps_the_connection() {
    let server = start(hook(|_, verb, nth| match (verb, nth) {
        ("RCPT", 1) => Hook::Reply("550 5.1.1 no such user\r\n".to_owned()),
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let mut d = draft();
    d.cc = mailboxes(Field::Cc, vec![("gone@example.com".to_owned(), None)]).unwrap();
    let error = mailer.send(d).await.unwrap_err();
    assert_eq!(error.kind, Kind::Rejected);
    assert_eq!(
        (error.code, error.enhanced.as_deref()),
        (Some(550), Some("5.1.1"))
    );
    assert_eq!(
        (error.field, error.index, error.attempts),
        (Some(Field::Cc), Some(0), 1)
    );
    assert!(!format!("{error}").contains("gone"));
    assert!(server.state.saw("RSET"));
    assert!(server.state.messages.lock().unwrap().is_empty());
    send(&mailer).await.unwrap();
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn retries_stop_at_the_configured_count() {
    let server = start(hook(|_, verb, _| match verb {
        "MAIL" => Hook::Reply("451 4.3.0 later\r\n".to_owned()),
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let error = send(&mailer).await.unwrap_err();
    assert_eq!((error.kind, error.attempts), (Kind::Temporary, 3));
    let stats = mailer.stats();
    assert_eq!((stats.sent, stats.failed, stats.retries), (0, 1, 2));
}

#[tokio::test]
async fn a_silent_greeting_times_out_and_is_retried() {
    let server = start(hook(|conn, verb, _| match (conn, verb) {
        (0, "CONNECT") => Hook::Stall,
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let sent = send(&mailer).await.unwrap();
    assert_eq!(sent.attempts, 2);
}

#[tokio::test]
async fn no_answer_after_the_end_of_data_is_never_retried() {
    let server = start(hook(|_, verb, _| match verb {
        "END" => Hook::Stall,
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let error = send(&mailer).await.unwrap_err();
    assert_eq!(
        (error.kind, error.possibly_sent, error.attempts),
        (Kind::Timeout, true, 1)
    );
    assert_eq!(server.state.messages.lock().unwrap().len(), 1);
    assert_eq!(mailer.stats().connections_open, 0);
}

#[tokio::test]
async fn a_server_that_never_answers_ends_at_the_overall_timeout() {
    let server = start(hook(|_, verb, _| match verb {
        "CONNECT" => Hook::Stall,
        _ => Hook::Normal,
    }))
    .await;
    let mut config = smtp(&server, Security::None);
    config.command_timeout = Duration::from_secs(30);
    config.connect_timeout = Duration::from_millis(100);
    let mailer = Mailer::new(Config {
        backend: Backend::Smtp(Box::new(config)),
        limits: Limits::default(),
        message_id_domain: "mail.example.com".to_owned(),
        timeout: Duration::from_millis(300),
    })
    .unwrap();
    let started = std::time::Instant::now();
    let error = send(&mailer).await.unwrap_err();
    assert_eq!(error.kind, Kind::Timeout);
    assert!(started.elapsed() < Duration::from_secs(3));
}

#[tokio::test]
async fn an_unreachable_server_is_a_connect_error() {
    let server = start(Options::default()).await;
    let config = smtp(&server, Security::None);
    drop(server);
    tokio::time::sleep(Duration::from_millis(50)).await;
    let error = send(&mailer_of(config)).await.unwrap_err();
    assert_eq!((error.kind, error.attempts), (Kind::Connect, 3));
}

#[tokio::test]
async fn an_oversized_reply_is_a_protocol_error() {
    let server = start(hook(|_, verb, _| match verb {
        "MAIL" => Hook::Reply(format!("250 {}\r\n", "x".repeat(5000))),
        _ => Hook::Normal,
    }))
    .await;
    let mut config = smtp(&server, Security::None);
    config.retry.retries = 0;
    let error = send(&mailer_of(config)).await.unwrap_err();
    assert_eq!(error.kind, Kind::Protocol);
}

#[tokio::test]
async fn a_message_over_the_advertised_size_is_not_sent() {
    let server = start(Options {
        size: Some(1000),
        ..Options::default()
    })
    .await;
    let mailer = mailer(&server, Security::None).await;
    let mut d = draft();
    d.text = Some("x".repeat(3000));
    let error = mailer.send(d).await.unwrap_err();
    assert_eq!(error.kind, Kind::LimitExceeded);
    assert!(!server.state.saw("MAIL"));
    send(&mailer).await.unwrap();
    assert!(server.state.saw("MAIL FROM:<from@example.com> SIZE="));
}

#[tokio::test]
async fn a_size_of_zero_declares_no_limit() {
    let server = start(Options {
        size: Some(0),
        ..Options::default()
    })
    .await;
    let mailer = mailer(&server, Security::None).await;
    send(&mailer).await.unwrap();
    assert!(server.state.saw("MAIL FROM:<from@example.com> SIZE="));
}

#[tokio::test]
async fn connections_are_reused_and_rotated() {
    let server = start(Options::default()).await;
    let mut config = smtp(&server, Security::None);
    let mailer = mailer_of(smtp(&server, Security::None));
    send(&mailer).await.unwrap();
    send(&mailer).await.unwrap();
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 1);
    let stats = mailer.stats();
    assert_eq!((stats.connections_open, stats.connections_idle), (1, 1));

    config.pool.max_messages_per_connection = 1;
    let rotating = mailer_of(config);
    send(&rotating).await.unwrap();
    send(&rotating).await.unwrap();
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 3);
    assert_eq!(rotating.stats().connections_open, 0);
}

#[tokio::test]
async fn idle_connections_expire() {
    let server = start(Options::default()).await;
    let mut config = smtp(&server, Security::None);
    config.pool.idle_timeout = Duration::from_millis(50);
    let mailer = mailer_of(config);
    send(&mailer).await.unwrap();
    tokio::time::sleep(Duration::from_millis(120)).await;
    send(&mailer).await.unwrap();
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn a_connection_the_server_closed_is_replaced_without_counting_a_retry() {
    let server = start(hook(|conn, verb, _| match (conn, verb) {
        (0, "END") => Hook::ReplyClose("250 2.0.0 queued\r\n".to_owned()),
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    assert_eq!(send(&mailer).await.unwrap().attempts, 1);
    tokio::time::sleep(Duration::from_millis(50)).await;
    let sent = send(&mailer).await.unwrap();
    assert_eq!(sent.attempts, 1);
    assert_eq!(mailer.stats().retries, 0);
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn the_pool_never_exceeds_its_size() {
    let server = start(hook(|_, verb, _| match verb {
        "DATA" => Hook::Delay(80),
        _ => Hook::Normal,
    }))
    .await;
    let mut config = smtp(&server, Security::None);
    config.pool.wait = Duration::from_secs(5);
    config.command_timeout = Duration::from_secs(2);
    let mailer = Arc::new(mailer_of(config));
    let tasks: Vec<_> = (0..8)
        .map(|_| {
            let mailer = Arc::clone(&mailer);
            tokio::spawn(async move { mailer.send(draft()).await })
        })
        .collect();
    for task in tasks {
        task.await.unwrap().unwrap();
    }
    assert!(server.state.peak.load(Ordering::SeqCst) <= 2);
    assert_eq!(server.state.messages.lock().unwrap().len(), 8);
    assert!(mailer.stats().connections_open <= 2);
}

#[tokio::test]
async fn a_saturated_pool_answers_busy_after_the_wait() {
    let server = start(hook(|_, verb, nth| match (verb, nth) {
        ("DATA", 0) => Hook::Delay(600),
        _ => Hook::Normal,
    }))
    .await;
    let mut config = smtp(&server, Security::None);
    config.pool.max_connections = 1;
    config.pool.wait = Duration::from_millis(80);
    config.command_timeout = Duration::from_secs(3);
    let mailer = Arc::new(mailer_of(config));
    let first = {
        let mailer = Arc::clone(&mailer);
        tokio::spawn(async move { mailer.send(draft()).await })
    };
    eventually(|| async { server.state.count("DATA") == 1 }).await;
    let error = send(&mailer).await.unwrap_err();
    assert_eq!(error.kind, Kind::Busy);
    first.await.unwrap().unwrap();
}

#[tokio::test]
async fn a_cancelled_send_does_not_return_its_connection_to_the_pool() {
    let server = start(hook(|_, verb, nth| match (verb, nth) {
        ("DATA", 0) => Hook::Stall,
        _ => Hook::Normal,
    }))
    .await;
    let mailer = mailer(&server, Security::None).await;
    let cancelled = tokio::time::timeout(Duration::from_millis(100), send(&mailer)).await;
    assert!(cancelled.is_err());
    let stats = mailer.stats();
    assert_eq!((stats.connections_open, stats.connections_idle), (0, 0));
    send(&mailer).await.unwrap();
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 2);
    assert_eq!(server.state.messages.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn closing_says_goodbye_and_refuses_further_use() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    send(&mailer).await.unwrap();
    mailer.close().await;
    mailer.close().await;
    eventually(|| async { server.state.saw("QUIT") }).await;
    assert_eq!(send(&mailer).await.unwrap_err().kind, Kind::Closed);
    assert_eq!(mailer.check().await.unwrap_err().kind, Kind::Closed);
    assert_eq!(mailer.stats().connections_open, 0);
}

#[tokio::test]
async fn check_opens_a_fresh_connection_and_reports_authentication_errors() {
    let server = start(Options {
        auth: Some("PLAIN"),
        ..Options::default()
    })
    .await;
    let mut config = smtp(&server, Security::None);
    config.credentials = login();
    let good = mailer_of(config);
    good.check().await.unwrap();
    good.check().await.unwrap();
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 2);
    assert!(server.state.saw("QUIT"));
    assert!(!server.state.saw("MAIL"));

    let bad_server = start(Options {
        auth: Some("PLAIN"),
        password: "other",
        ..Options::default()
    })
    .await;
    let mut config = smtp(&bad_server, Security::None);
    config.credentials = login();
    assert_eq!(
        mailer_of(config).check().await.unwrap_err().kind,
        Kind::Auth
    );
}

#[tokio::test]
async fn the_capture_backend_keeps_the_bytes_and_touches_no_network() {
    let mailer = Mailer::new(Config {
        backend: Backend::Capture,
        limits: Limits::default(),
        message_id_domain: "mail.example.com".to_owned(),
        timeout: Duration::from_secs(1),
    })
    .unwrap();
    let sent = send(&mailer).await.unwrap();
    assert_eq!((sent.attempts, sent.recipients.len()), (1, 1));
    let raw = String::from_utf8(sent.raw.unwrap()).unwrap();
    assert!(raw.contains("Subject: Hello") && raw.contains(&sent.message_id));
    mailer.check().await.unwrap();
    let stats = mailer.stats();
    assert_eq!((stats.sent, stats.connections_open), (1, 0));
    let invalid = mailer
        .send(Draft {
            text: None,
            ..draft()
        })
        .await
        .unwrap_err();
    assert_eq!(invalid.kind, Kind::InvalidMessage);
    assert_eq!(mailer.stats().failed, 1);
}

#[tokio::test]
async fn large_messages_are_rendered_off_the_event_loop_and_arrive_intact() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    let mut d = draft();
    d.attachments = vec![hinoki_mail::AttachmentPart {
        filename: "big.bin".to_owned(),
        content_type: "application/octet-stream".to_owned(),
        content: vec![0x5a; 300_000],
    }];
    mailer.send(d).await.unwrap();
    let message = server.state.messages.lock().unwrap()[0].clone();
    assert!(message.len() > 400_000);
    assert!(message.is_ascii());
}

#[tokio::test]
async fn limits_refuse_before_any_connection() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    let mut d = draft();
    d.to = (0..101)
        .map(|n| {
            mailboxes(Field::To, vec![(format!("u{n}@example.com"), None)])
                .unwrap()
                .remove(0)
        })
        .collect();
    let error = mailer.send(d).await.unwrap_err();
    assert_eq!(
        (error.kind, error.field),
        (Kind::LimitExceeded, Some(Field::To))
    );
    assert_eq!(server.state.connections.load(Ordering::SeqCst), 0);
}

#[test]
fn unsafe_or_invalid_settings_are_refused_when_building() {
    let probe = |edit: &dyn Fn(&mut SmtpConfig)| {
        let mut config = SmtpConfig {
            host: "127.0.0.1".to_owned(),
            port: 2525,
            security: Security::None,
            credentials: None,
            allow_insecure: false,
            tls_ca_file: None,
            helo_name: None,
            connect_timeout: Duration::from_secs(1),
            command_timeout: Duration::from_secs(1),
            data_timeout: Duration::from_secs(1),
            pool: PoolConfig {
                max_connections: 2,
                wait: Duration::from_secs(1),
                idle_timeout: Duration::from_secs(60),
                max_messages_per_connection: 10,
            },
            retry: RetryPolicy {
                retries: 1,
                base: Duration::from_millis(10),
                max: Duration::from_millis(20),
            },
        };
        edit(&mut config);
        Mailer::new(Config {
            backend: Backend::Smtp(Box::new(config)),
            limits: Limits::default(),
            message_id_domain: "mail.example.com".to_owned(),
            timeout: Duration::from_secs(5),
        })
        .map(|_| ())
        .map_err(|e| e.to_string())
    };
    assert!(probe(&|_| {}).is_ok());
    assert!(probe(&|c| c.host = "mail.example.com".to_owned()).is_err());
    assert!(probe(&|c| {
        c.host = "mail.example.com".to_owned();
        c.allow_insecure = true;
    })
    .is_ok());
    assert!(probe(&|c| c.pool.max_connections = 0).is_err());
    assert!(probe(&|c| c.pool.max_connections = 65).is_err());
    assert!(probe(&|c| c.retry.retries = 6).is_err());
    assert!(probe(&|c| c.connect_timeout = Duration::ZERO).is_err());
    assert!(probe(&|c| c.helo_name = Some("bad name".to_owned())).is_err());
    assert!(probe(&|c| c.tls_ca_file = Some(PathBuf::from("/nonexistent/ca.pem"))).is_err());
    let big = Path::new(env!("CARGO_TARGET_TMPDIR")).join("oversize-ca.pem");
    std::fs::write(&big, vec![b'#'; 1024 * 1024 + 1]).unwrap();
    let message = probe(&|c| c.tls_ca_file = Some(big.clone())).unwrap_err();
    assert!(message.contains("1 MiB"));
    std::fs::remove_file(&big).unwrap();
}

#[derive(Default)]
struct Counts {
    sent: AtomicUsize,
    retried: AtomicUsize,
    failed: Mutex<Vec<Kind>>,
}

impl Observer for Counts {
    fn sent(&self, _: f64) {
        self.sent.fetch_add(1, Ordering::SeqCst);
    }

    fn failed(&self, kind: Kind, _: f64) {
        self.failed.lock().unwrap().push(kind);
    }

    fn retried(&self) {
        self.retried.fetch_add(1, Ordering::SeqCst);
    }
}

#[tokio::test]
async fn the_observer_sees_every_outcome_including_validation_failures() {
    let server = start(Options::default()).await;
    let mailer = mailer(&server, Security::None).await;
    let seen = Arc::new(Counts::default());
    mailer.attach(seen.clone());
    send(&mailer).await.unwrap();
    mailer
        .send(Draft {
            text: None,
            ..draft()
        })
        .await
        .unwrap_err();
    assert_eq!(seen.sent.load(Ordering::SeqCst), 1);
    assert_eq!(*seen.failed.lock().unwrap(), [Kind::InvalidMessage]);
}
