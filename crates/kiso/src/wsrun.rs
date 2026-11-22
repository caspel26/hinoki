//! The server side of one upgraded WebSocket connection: reads frames, writes
//! frames, and keeps the connection honest. Everything the application sees is
//! two bounded channels (see [`Session`]); the byte-level work (framing,
//! fragmentation, UTF-8, ping, pong, close, limits, timeouts) happens here.
//!
//! * **Backpressure, inbound:** a complete message goes into a bounded channel.
//!   While it is full the driver holds the one message it could not deliver and
//!   stops reading the socket, so a client that outruns the application is
//!   slowed by TCP, not by memory.
//! * **Backpressure, outbound:** the application's sender waits when the queue is
//!   full, and a socket write that makes no progress for `write_timeout` ends
//!   the connection.
//! * **Liveness:** a ping every `ping_interval`; no frame at all within
//!   `pong_timeout` of it ends the connection (1006). `idle_timeout`, when set,
//!   closes (1001) a connection whose client sent no data message for that long.
//! * **Close:** whoever closes first sends a close frame; the other side's
//!   answer is awaited for `close_timeout`. A stop (shutdown or reload) closes
//!   with 1001.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, watch, OwnedSemaphorePermit};
use tokio::time::{sleep_until, timeout, Instant};

use crate::headers::Header;
use crate::ws::{self, Decoder, Event, Fault};

const READ_CHUNK: usize = 16 * 1024;
const FAR: Duration = Duration::from_secs(86_400);

/// The limits and timers of one connection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Largest message after reassembly, in bytes.
    pub max_message: usize,
    /// How often the server pings; `None` never does.
    pub ping_interval: Option<Duration>,
    /// How long after a ping any frame from the client may take to arrive.
    pub pong_timeout: Duration,
    /// Close a client that sent no data message for this long; `None` never does.
    pub idle_timeout: Option<Duration>,
    /// How long to wait for the peer's close frame after sending ours.
    pub close_timeout: Duration,
    /// How long one write may make no progress.
    pub write_timeout: Duration,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            max_message: 1024 * 1024,
            ping_interval: Some(Duration::from_secs(20)),
            pong_timeout: Duration::from_secs(20),
            idle_timeout: None,
            close_timeout: Duration::from_secs(5),
            write_timeout: Duration::from_secs(30),
        }
    }
}

/// A data message, either way.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Message {
    Text(String),
    Binary(Vec<u8>),
}

/// What the application queues for the client.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outbound {
    Message(Message),
    /// Send everything queued before it, then close with this code and reason.
    Close(u16, String),
}

/// A close the connection ended with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Closed {
    pub code: u16,
    pub reason: String,
}

/// What the driver tells the application once the inbound channel is closed.
#[derive(Debug, Default)]
pub struct Shared {
    closed: Mutex<Option<Closed>>,
}

impl Shared {
    /// How the connection ended; `None` while it is open.
    pub fn closed(&self) -> Option<Closed> {
        self.closed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    fn record(&self, code: u16, reason: &str) {
        let mut slot = self
            .closed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        slot.get_or_insert_with(|| Closed {
            code,
            reason: reason.to_owned(),
        });
    }
}

/// What the connection hands the driver once the upgrade is done: the ends of
/// the two channels it owns, plus what it needs to report.
#[derive(Debug)]
pub struct Session {
    pub config: Config,
    pub inbound: mpsc::Sender<Message>,
    pub outbound: mpsc::Receiver<Outbound>,
    pub shared: Arc<Shared>,
    /// A connection slot held for as long as the session runs.
    pub hold: Option<OwnedSemaphorePermit>,
}

/// An accepted handshake: the `101` and the session that follows it.
#[derive(Debug)]
pub struct Accepted {
    pub subprotocol: Option<String>,
    pub headers: Vec<Header>,
    pub session: Session,
}

enum State {
    Open,
    /// Our close frame is sent; the peer's is awaited until this instant.
    Closing(Instant),
}

enum Flow {
    Continue,
    /// A message could not be delivered yet: stop reading until it is.
    Pause,
    End,
}

struct Driver<S> {
    stream: S,
    decoder: Decoder,
    config: Config,
    inbound: Option<mpsc::Sender<Message>>,
    outbound: mpsc::Receiver<Outbound>,
    shared: Arc<Shared>,
    pending: Option<Message>,
    state: State,
    ping_due: Option<Instant>,
    pong_due: Option<Instant>,
    idle_due: Option<Instant>,
}

/// Run the connection until it ends: the peer closes, a limit is hit, the
/// stream fails or `stop` fires (then with 1001). `stream` must carry the bytes
/// after the handshake, unscanned.
pub async fn run<S>(stream: S, session: Session, mut stop: watch::Receiver<bool>)
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let Session {
        config,
        inbound,
        outbound,
        shared,
        hold: _hold,
    } = session;
    let now = Instant::now();
    let mut driver = Driver {
        stream,
        decoder: Decoder::new(config.max_message),
        ping_due: config.ping_interval.map(|every| now + every),
        idle_due: config.idle_timeout.map(|idle| now + idle),
        pong_due: None,
        config,
        inbound: Some(inbound),
        outbound,
        shared,
        pending: None,
        state: State::Open,
    };
    driver.drive(&mut stop).await;
    driver.finish().await;
}

async fn stopped(stop: &mut watch::Receiver<bool>) {
    let _ = stop.wait_for(|requested| *requested).await;
}

async fn room(sender: Option<mpsc::Sender<Message>>) -> Option<mpsc::OwnedPermit<Message>> {
    match sender {
        Some(sender) => sender.reserve_owned().await.ok(),
        None => std::future::pending().await,
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> Driver<S> {
    fn open(&self) -> bool {
        matches!(self.state, State::Open)
    }

    async fn drive(&mut self, stop: &mut watch::Receiver<bool>) {
        let mut buffer = vec![0_u8; READ_CHUNK];
        loop {
            let deadline = self.next_deadline();
            let flow = tokio::select! {
                biased;
                () = stopped(stop), if self.open() => {
                    self.begin_close(ws::GOING_AWAY, "server shutdown").await
                }
                permit = room(self.inbound.clone()), if self.pending.is_some() => {
                    self.deliver(permit).await
                }
                read = self.stream.read(&mut buffer), if self.pending.is_none() => {
                    self.on_read(read, &buffer).await
                }
                item = self.outbound.recv(), if self.open() => self.on_outbound(item).await,
                () = sleep_until(deadline) => self.on_timer().await,
            };
            if matches!(flow, Flow::End) {
                return;
            }
        }
    }

    fn next_deadline(&self) -> Instant {
        let closing = match self.state {
            State::Closing(at) => Some(at),
            State::Open => None,
        };
        // While the application is behind, the peer is not being read: its pong
        // or message may sit unread in the socket, so those timers wait.
        let (pong, idle) = if self.pending.is_some() {
            (None, None)
        } else {
            (self.pong_due, self.idle_due)
        };
        [self.ping_due, pong, idle, closing]
            .into_iter()
            .flatten()
            .min()
            .unwrap_or_else(|| Instant::now() + FAR)
    }

    async fn write(&mut self, bytes: &[u8]) -> bool {
        let sent = timeout(self.config.write_timeout, async {
            self.stream.write_all(bytes).await?;
            self.stream.flush().await
        })
        .await;
        matches!(sent, Ok(Ok(())))
    }

    /// Send our close frame and wait for the peer's.
    async fn begin_close(&mut self, code: u16, reason: &str) -> Flow {
        self.shared.record(code, reason);
        self.pending = None;
        self.ping_due = None;
        self.pong_due = None;
        self.idle_due = None;
        if !self.write(&ws::close_frame(code, reason)).await {
            return Flow::End;
        }
        self.state = State::Closing(Instant::now() + self.config.close_timeout);
        Flow::Continue
    }

    async fn deliver(&mut self, permit: Option<mpsc::OwnedPermit<Message>>) -> Flow {
        // Reading resumes: give the peer a full period to answer or speak.
        let now = Instant::now();
        if self.pong_due.is_some() {
            self.pong_due = Some(now + self.config.pong_timeout);
        }
        if self.idle_due.is_some() {
            self.idle_due = self.config.idle_timeout.map(|idle| now + idle);
        }
        match (permit, self.pending.take()) {
            (Some(permit), Some(message)) => {
                permit.send(message);
                self.pump().await
            }
            // The application stopped listening: later messages are dropped.
            _ => {
                self.inbound = None;
                self.pump().await
            }
        }
    }

    async fn on_read(&mut self, read: std::io::Result<usize>, buffer: &[u8]) -> Flow {
        match read {
            Ok(size) if size > 0 => {
                self.decoder.push(&buffer[..size]);
                self.pump().await
            }
            _ => {
                self.shared.record(ws::ABNORMAL, "connection lost");
                Flow::End
            }
        }
    }

    async fn on_outbound(&mut self, item: Option<Outbound>) -> Flow {
        match item {
            Some(Outbound::Message(Message::Text(text))) => {
                self.send(ws::TEXT, text.as_bytes()).await
            }
            Some(Outbound::Message(Message::Binary(data))) => self.send(ws::BINARY, &data).await,
            Some(Outbound::Close(code, reason)) => self.begin_close(code, &reason).await,
            None => self.begin_close(ws::INTERNAL, "handler ended").await,
        }
    }

    async fn send(&mut self, opcode: u8, payload: &[u8]) -> Flow {
        if self.write(&ws::frame(opcode, payload)).await {
            Flow::Continue
        } else {
            Flow::End
        }
    }

    async fn on_timer(&mut self) -> Flow {
        let now = Instant::now();
        let due = |at: Option<Instant>| at.is_some_and(|at| at <= now);
        if let State::Closing(at) = self.state {
            return if at <= now { Flow::End } else { Flow::Continue };
        }
        let waiting = self.pending.is_some();
        if !waiting && due(self.pong_due) {
            self.shared.record(ws::ABNORMAL, "no answer to ping");
            return Flow::End;
        }
        if !waiting && due(self.idle_due) {
            return self.begin_close(ws::GOING_AWAY, "idle").await;
        }
        if due(self.ping_due) {
            self.ping_due = self.config.ping_interval.map(|every| now + every);
            self.pong_due.get_or_insert(now + self.config.pong_timeout);
            return self.send(ws::PING, b"").await;
        }
        Flow::Continue
    }

    /// Handle every event the decoder has ready, until a message cannot be
    /// delivered yet or the connection ends.
    async fn pump(&mut self) -> Flow {
        loop {
            let event = match self.decoder.next() {
                Ok(Some(event)) => event,
                Ok(None) => return Flow::Continue,
                Err(fault) => return self.refuse(fault).await,
            };
            self.pong_due = None;
            match self.on_event(event).await {
                Flow::Continue => {}
                other => return other,
            }
        }
    }

    async fn refuse(&mut self, fault: Fault) -> Flow {
        self.shared.record(fault.code(), fault.reason());
        self.write(&ws::close_frame(fault.code(), fault.reason()))
            .await;
        Flow::End
    }

    async fn on_event(&mut self, event: Event) -> Flow {
        match event {
            Event::Text(text) => self.message(Message::Text(text)),
            Event::Binary(data) => self.message(Message::Binary(data)),
            Event::Ping(payload) if self.open() => self.send(ws::PONG, &payload).await,
            Event::Ping(_) | Event::Pong(_) => Flow::Continue,
            Event::Close(closing) => self.on_peer_close(closing).await,
        }
    }

    fn message(&mut self, message: Message) -> Flow {
        if !self.open() {
            return Flow::Continue;
        }
        self.idle_due = self.config.idle_timeout.map(|idle| Instant::now() + idle);
        let Some(inbound) = &self.inbound else {
            return Flow::Continue;
        };
        match inbound.try_send(message) {
            Ok(()) => Flow::Continue,
            Err(mpsc::error::TrySendError::Full(message)) => {
                self.pending = Some(message);
                Flow::Pause
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                self.inbound = None;
                Flow::Continue
            }
        }
    }

    async fn on_peer_close(&mut self, closing: Option<(u16, String)>) -> Flow {
        let (code, reason) = closing.unwrap_or((ws::NO_STATUS, String::new()));
        self.shared.record(code, &reason);
        if self.open() {
            let echo = if code == ws::NO_STATUS {
                ws::NORMAL
            } else {
                code
            };
            self.write(&ws::close_frame(echo, "")).await;
        }
        Flow::End
    }

    async fn finish(&mut self) {
        self.inbound = None;
        self.shared.record(ws::ABNORMAL, "connection lost");
        let _ = timeout(self.config.close_timeout, self.stream.shutdown()).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{duplex, DuplexStream};

    struct Rig {
        client: DuplexStream,
        to_client: mpsc::Sender<Outbound>,
        from_client: mpsc::Receiver<Message>,
        shared: Arc<Shared>,
        stop: watch::Sender<bool>,
        task: tokio::task::JoinHandle<()>,
    }

    fn rig(config: Config, inbound_capacity: usize) -> Rig {
        let (client, server) = duplex(64 * 1024);
        let (inbound, from_client) = mpsc::channel(inbound_capacity);
        let (to_client, outbound) = mpsc::channel(8);
        let shared = Arc::new(Shared::default());
        let (stop, stopped) = watch::channel(false);
        let session = Session {
            config,
            inbound,
            outbound,
            shared: Arc::clone(&shared),
            hold: None,
        };
        let task = tokio::spawn(run(server, session, stopped));
        Rig {
            client,
            to_client,
            from_client,
            shared,
            stop,
            task,
        }
    }

    fn quiet() -> Config {
        Config {
            max_message: 1024,
            ping_interval: None,
            idle_timeout: None,
            close_timeout: Duration::from_millis(200),
            ..Config::default()
        }
    }

    /// One frame a server sent: opcode and payload.
    async fn frame_from(client: &mut DuplexStream) -> Option<(u8, Vec<u8>)> {
        let mut head = [0_u8; 2];
        client.read_exact(&mut head).await.ok()?;
        assert_eq!(head[1] & 0x80, 0, "server frames are not masked");
        let length = match head[1] & 0x7F {
            126 => {
                let mut raw = [0; 2];
                client.read_exact(&mut raw).await.ok()?;
                usize::from(u16::from_be_bytes(raw))
            }
            127 => {
                let mut raw = [0; 8];
                client.read_exact(&mut raw).await.ok()?;
                usize::try_from(u64::from_be_bytes(raw)).unwrap()
            }
            short => usize::from(short),
        };
        let mut payload = vec![0; length];
        client.read_exact(&mut payload).await.ok()?;
        Some((head[0] & 0x0F, payload))
    }

    fn close_payload(code: u16, reason: &str) -> Vec<u8> {
        let mut payload = code.to_be_bytes().to_vec();
        payload.extend_from_slice(reason.as_bytes());
        payload
    }

    async fn send(client: &mut DuplexStream, opcode: u8, payload: &[u8]) {
        let bytes = ws::masked_frame(opcode, true, payload, [9, 8, 7, 6]);
        client.write_all(&bytes).await.unwrap();
    }

    #[tokio::test]
    async fn messages_flow_both_ways() {
        let mut rig = rig(quiet(), 4);
        send(&mut rig.client, ws::TEXT, b"hello").await;
        send(&mut rig.client, ws::BINARY, &[1, 2]).await;
        assert_eq!(
            rig.from_client.recv().await,
            Some(Message::Text("hello".into()))
        );
        assert_eq!(
            rig.from_client.recv().await,
            Some(Message::Binary(vec![1, 2]))
        );
        rig.to_client
            .send(Outbound::Message(Message::Text("hi".into())))
            .await
            .unwrap();
        assert_eq!(
            frame_from(&mut rig.client).await,
            Some((ws::TEXT, b"hi".to_vec()))
        );
    }

    #[tokio::test]
    async fn a_ping_is_answered_with_the_same_payload() {
        let mut rig = rig(quiet(), 4);
        send(&mut rig.client, ws::PING, b"are you there").await;
        assert_eq!(
            frame_from(&mut rig.client).await,
            Some((ws::PONG, b"are you there".to_vec()))
        );
    }

    #[tokio::test]
    async fn a_client_close_is_echoed_and_recorded() {
        let mut rig = rig(quiet(), 4);
        send(&mut rig.client, ws::CLOSE, &close_payload(4001, "bye")).await;
        assert_eq!(
            frame_from(&mut rig.client).await,
            Some((ws::CLOSE, close_payload(4001, "")))
        );
        rig.task.await.unwrap();
        assert_eq!(
            rig.shared.closed(),
            Some(Closed {
                code: 4001,
                reason: "bye".into()
            })
        );
        assert_eq!(rig.from_client.recv().await, None);
    }

    #[tokio::test]
    async fn the_application_closes_after_its_queued_messages() {
        let mut rig = rig(quiet(), 4);
        rig.to_client
            .send(Outbound::Message(Message::Text("last".into())))
            .await
            .unwrap();
        rig.to_client
            .send(Outbound::Close(1000, "done".into()))
            .await
            .unwrap();
        assert_eq!(frame_from(&mut rig.client).await.unwrap().0, ws::TEXT);
        assert_eq!(
            frame_from(&mut rig.client).await,
            Some((ws::CLOSE, close_payload(1000, "done")))
        );
        send(&mut rig.client, ws::CLOSE, &close_payload(1000, "")).await;
        rig.task.await.unwrap();
    }

    #[tokio::test]
    async fn data_after_our_close_is_discarded() {
        let mut rig = rig(quiet(), 4);
        rig.to_client
            .send(Outbound::Close(1000, String::new()))
            .await
            .unwrap();
        assert_eq!(frame_from(&mut rig.client).await.unwrap().0, ws::CLOSE);
        send(&mut rig.client, ws::TEXT, b"late").await;
        send(&mut rig.client, ws::CLOSE, &close_payload(1000, "")).await;
        rig.task.await.unwrap();
        assert_eq!(rig.from_client.recv().await, None);
    }

    #[tokio::test]
    async fn a_stop_closes_with_1001_and_waits_for_the_peer() {
        let mut rig = rig(quiet(), 4);
        rig.stop.send(true).unwrap();
        assert_eq!(
            frame_from(&mut rig.client).await,
            Some((ws::CLOSE, close_payload(1001, "server shutdown")))
        );
        send(&mut rig.client, ws::CLOSE, &close_payload(1001, "")).await;
        rig.task.await.unwrap();
        assert_eq!(rig.shared.closed().unwrap().code, 1001);
    }

    #[tokio::test]
    async fn a_silent_peer_does_not_hold_a_close_forever() {
        let rig = rig(quiet(), 4);
        rig.stop.send(true).unwrap();
        timeout(Duration::from_secs(5), rig.task)
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn a_message_over_the_limit_closes_with_1009() {
        let mut rig = rig(quiet(), 4);
        send(&mut rig.client, ws::BINARY, &[0; 2000]).await;
        assert_eq!(frame_from(&mut rig.client).await.unwrap().0, ws::CLOSE);
        rig.task.await.unwrap();
        assert_eq!(rig.shared.closed().unwrap().code, 1009);
    }

    #[tokio::test]
    async fn malformed_frames_close_with_their_code() {
        let mut rig = rig(quiet(), 4);
        rig.client.write_all(&[0x81, 0x01, b'x']).await.unwrap();
        let (opcode, payload) = frame_from(&mut rig.client).await.unwrap();
        assert_eq!(
            (opcode, &payload[..2]),
            (ws::CLOSE, &1002_u16.to_be_bytes()[..])
        );
        rig.task.await.unwrap();

        let mut rig = self::rig(quiet(), 4);
        send(&mut rig.client, ws::TEXT, &[0xC3, 0x28]).await;
        let (_, payload) = frame_from(&mut rig.client).await.unwrap();
        assert_eq!(&payload[..2], &1007_u16.to_be_bytes());
    }

    #[tokio::test]
    async fn a_full_inbound_queue_stops_the_reading() {
        let mut rig = rig(quiet(), 1);
        for number in 0..4 {
            send(&mut rig.client, ws::TEXT, format!("m{number}").as_bytes()).await;
        }
        // Give the driver time to fill the queue and stop at the next message.
        tokio::time::sleep(Duration::from_millis(50)).await;
        send(&mut rig.client, ws::PING, b"p").await;
        assert!(
            timeout(Duration::from_millis(100), frame_from(&mut rig.client))
                .await
                .is_err(),
            "the driver kept reading while a message was undelivered"
        );
        for number in 0..4 {
            let expected = Message::Text(format!("m{number}"));
            assert_eq!(rig.from_client.recv().await, Some(expected));
        }
        assert_eq!(frame_from(&mut rig.client).await.unwrap().0, ws::PONG);
    }

    #[tokio::test(start_paused = true)]
    async fn an_unanswered_ping_ends_the_connection() {
        let config = Config {
            ping_interval: Some(Duration::from_secs(10)),
            pong_timeout: Duration::from_secs(5),
            ..quiet()
        };
        let mut rig = rig(config, 4);
        assert_eq!(frame_from(&mut rig.client).await.unwrap().0, ws::PING);
        rig.task.await.unwrap();
        assert_eq!(rig.shared.closed().unwrap().code, ws::ABNORMAL);
    }

    #[tokio::test(start_paused = true)]
    async fn any_frame_counts_as_the_answer_to_a_ping() {
        let config = Config {
            ping_interval: Some(Duration::from_secs(10)),
            pong_timeout: Duration::from_secs(5),
            ..quiet()
        };
        let mut rig = rig(config, 4);
        for _ in 0..3 {
            assert_eq!(frame_from(&mut rig.client).await.unwrap().0, ws::PING);
            send(&mut rig.client, ws::PONG, b"").await;
        }
        assert!(!rig.task.is_finished());
    }

    #[tokio::test(start_paused = true)]
    async fn an_idle_client_is_closed_with_1001() {
        let config = Config {
            idle_timeout: Some(Duration::from_secs(30)),
            ..quiet()
        };
        let mut rig = rig(config, 4);
        let (opcode, payload) = frame_from(&mut rig.client).await.unwrap();
        assert_eq!((opcode, payload), (ws::CLOSE, close_payload(1001, "idle")));
    }

    #[tokio::test(start_paused = true)]
    async fn a_slow_application_does_not_get_the_peer_dropped() {
        let config = Config {
            ping_interval: Some(Duration::from_secs(10)),
            pong_timeout: Duration::from_secs(5),
            idle_timeout: Some(Duration::from_secs(30)),
            ..quiet()
        };
        let mut rig = rig(config, 1);
        for text in ["a", "b", "c"] {
            send(&mut rig.client, ws::TEXT, text.as_bytes()).await;
        }
        // The application reads nothing for far longer than every timer.
        tokio::time::sleep(Duration::from_secs(120)).await;
        assert!(!rig.task.is_finished());
        assert!(rig.shared.closed().is_none());
        for expected in ["a", "b", "c"] {
            let got = rig.from_client.recv().await.unwrap();
            assert_eq!(got, Message::Text(expected.into()));
        }
    }

    #[tokio::test]
    async fn a_dropped_application_closes_with_1011() {
        let mut rig = rig(quiet(), 4);
        drop(std::mem::replace(&mut rig.to_client, mpsc::channel(1).0));
        let (_, payload) = frame_from(&mut rig.client).await.unwrap();
        assert_eq!(&payload[..2], &1011_u16.to_be_bytes());
    }

    #[tokio::test]
    async fn a_lost_connection_is_recorded_as_1006() {
        let rig = rig(quiet(), 4);
        drop(rig.client);
        rig.task.await.unwrap();
        assert_eq!(rig.shared.closed().unwrap().code, ws::ABNORMAL);
    }

    #[tokio::test]
    async fn a_fragmented_message_arrives_whole() {
        let mut rig = rig(quiet(), 4);
        let first = ws::masked_frame(ws::TEXT, false, b"he", [1, 2, 3, 4]);
        let second = ws::masked_frame(ws::CONTINUATION, true, b"llo", [5, 6, 7, 8]);
        rig.client.write_all(&first).await.unwrap();
        rig.client.write_all(&second).await.unwrap();
        assert_eq!(
            rig.from_client.recv().await,
            Some(Message::Text("hello".into()))
        );
    }
}
