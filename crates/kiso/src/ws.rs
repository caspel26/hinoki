//! WebSocket (RFC 6455) on the server side: the opening handshake and the frame
//! codec. Pure functions and a byte-fed decoder, no I/O; the connection that
//! runs them is [`crate::wsrun`].
//!
//! The decoder is for frames a *client* sends, so it requires them to be masked,
//! refuses reserved bits (no extension is negotiated), bounds a message before
//! buffering its payload, and checks the text it assembles is UTF-8. Every
//! refusal is a [`Fault`] that carries the close code the peer is told.

use sha1::{Digest, Sha1};

use crate::headers::Header;

const GUID: &[u8] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub const CONTINUATION: u8 = 0x0;
pub const TEXT: u8 = 0x1;
pub const BINARY: u8 = 0x2;
pub const CLOSE: u8 = 0x8;
pub const PING: u8 = 0x9;
pub const PONG: u8 = 0xA;

/// Longest payload a control frame may carry (RFC 6455 5.5).
pub const MAX_CONTROL: usize = 125;

pub const NORMAL: u16 = 1000;
pub const GOING_AWAY: u16 = 1001;
pub const PROTOCOL: u16 = 1002;
pub const NOT_UTF8: u16 = 1007;
pub const TOO_BIG: u16 = 1009;
pub const INTERNAL: u16 = 1011;
/// Never sent: what a close without a status code, or a lost connection, reports.
pub const NO_STATUS: u16 = 1005;
pub const ABNORMAL: u16 = 1006;

/// The `Sec-WebSocket-Accept` value for a client's `Sec-WebSocket-Key`.
pub fn accept_key(key: &[u8]) -> String {
    let mut hasher = Sha1::new();
    hasher.update(key);
    hasher.update(GUID);
    base64(&hasher.finalize())
}

fn base64(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let word = chunk.iter().enumerate().fold(0u32, |word, (at, byte)| {
            word | u32::from(*byte) << (16 - 8 * at)
        });
        for step in 0..4 {
            if step <= chunk.len() {
                text.push(char::from(
                    ALPHABET[(word >> (18 - 6 * step) & 63) as usize],
                ));
            } else {
                text.push('=');
            }
        }
    }
    text
}

/// A client's valid `Sec-WebSocket-Key`: 16 random bytes in base64.
fn valid_key(key: &[u8]) -> bool {
    key.len() == 24
        && key.ends_with(b"==")
        && key[..22].iter().all(|byte| ALPHABET.contains(byte))
        && matches!(key[21], b'A' | b'Q' | b'g' | b'w')
}

/// What a request asks for in the way of an upgrade to WebSocket.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum Upgrade {
    /// Not a WebSocket upgrade: served as the plain request it is.
    #[default]
    Absent,
    Offer(Offer),
    /// A WebSocket upgrade with a missing, repeated or malformed key.
    BadKey,
    /// A WebSocket upgrade for a version other than 13.
    BadVersion,
}

/// A well-formed opening handshake.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Offer {
    pub key: String,
    /// The subprotocols the client offered, in its order of preference.
    pub protocols: Vec<String>,
}

fn values<'a>(headers: &'a [Header], name: &'a [u8]) -> impl Iterator<Item = &'a [u8]> {
    headers
        .iter()
        .filter(move |(candidate, _)| candidate == name)
        .map(|(_, value)| value.as_slice())
}

fn tokens(value: &[u8]) -> impl Iterator<Item = &[u8]> {
    value.split(|byte| *byte == b',').map(<[u8]>::trim_ascii)
}

fn has_token(headers: &[Header], name: &[u8], wanted: &[u8]) -> bool {
    values(headers, name)
        .flat_map(tokens)
        .any(|token| token.eq_ignore_ascii_case(wanted))
}

/// A subprotocol name is an HTTP token (RFC 9110 5.6.2).
fn is_token(text: &[u8]) -> bool {
    !text.is_empty()
        && text
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(byte))
}

/// Read the handshake of a request; header names are lowercase. Only an HTTP/1.1
/// `GET` that asks for `Upgrade: websocket` and `Connection: upgrade` is one;
/// anything else is [`Upgrade::Absent`].
pub fn inspect(method: &str, headers: &[Header]) -> Upgrade {
    let asks = method == "GET"
        && has_token(headers, b"upgrade", b"websocket")
        && has_token(headers, b"connection", b"upgrade");
    if !asks {
        return Upgrade::Absent;
    }
    let mut versions = values(headers, b"sec-websocket-version");
    if versions.next().map(<[u8]>::trim_ascii) != Some(b"13") || versions.next().is_some() {
        return Upgrade::BadVersion;
    }
    let mut keys = values(headers, b"sec-websocket-key");
    let key = keys.next().map(<[u8]>::trim_ascii);
    match (key, keys.next()) {
        (Some(key), None) if valid_key(key) => Upgrade::Offer(Offer {
            key: String::from_utf8_lossy(key).into_owned(),
            protocols: values(headers, b"sec-websocket-protocol")
                .flat_map(tokens)
                .filter(|token| is_token(token))
                .map(|token| String::from_utf8_lossy(token).into_owned())
                .collect(),
        }),
        _ => Upgrade::BadKey,
    }
}

/// Why a frame stream is refused, with the close code the peer is sent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    Protocol(&'static str),
    TooBig,
    NotUtf8,
}

impl Fault {
    pub const fn code(self) -> u16 {
        match self {
            Self::Protocol(_) => PROTOCOL,
            Self::TooBig => TOO_BIG,
            Self::NotUtf8 => NOT_UTF8,
        }
    }

    pub const fn reason(self) -> &'static str {
        match self {
            Self::Protocol(reason) => reason,
            Self::TooBig => "message too big",
            Self::NotUtf8 => "invalid UTF-8",
        }
    }
}

/// One complete message or control frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    Text(String),
    Binary(Vec<u8>),
    Ping(Vec<u8>),
    Pong(Vec<u8>),
    /// The status code and reason, when the frame carried them.
    Close(Option<(u16, String)>),
}

/// Whether a close code may appear on the wire (RFC 6455 7.4).
pub const fn sendable_code(code: u16) -> bool {
    matches!(code, 1000..=1003 | 1007..=1014 | 3000..=4999)
}

struct Head {
    fin: bool,
    opcode: u8,
    length: usize,
    mask: [u8; 4],
    size: usize,
}

struct Partial {
    text: bool,
    data: Vec<u8>,
}

/// Turns the bytes a client sends into events. Feed it with [`Decoder::push`]
/// and take events until it returns `None`.
pub struct Decoder {
    max_message: usize,
    buffer: Vec<u8>,
    partial: Option<Partial>,
}

impl Decoder {
    /// `max_message` bounds a message after reassembly.
    pub fn new(max_message: usize) -> Self {
        Self {
            max_message,
            buffer: Vec::new(),
            partial: None,
        }
    }

    pub fn push(&mut self, bytes: &[u8]) {
        self.buffer.extend_from_slice(bytes);
    }

    /// The next event, `None` when more bytes are needed.
    #[allow(
        clippy::should_implement_trait,
        reason = "yields a Result of Option, not an Iterator item"
    )]
    pub fn next(&mut self) -> Result<Option<Event>, Fault> {
        loop {
            let Some(head) = self.head()? else {
                return Ok(None);
            };
            let end = head.size + head.length;
            if self.buffer.len() < end {
                return Ok(None);
            }
            let mut payload = self.buffer[head.size..end].to_vec();
            self.buffer.drain(..end);
            unmask(&mut payload, head.mask);
            if let Some(event) = self.accept(head.fin, head.opcode, payload)? {
                return Ok(Some(event));
            }
        }
    }

    /// The header of the frame at the front, checked before its payload is awaited.
    fn head(&self) -> Result<Option<Head>, Fault> {
        let [first, second, ..] = self.buffer[..] else {
            return Ok(None);
        };
        let (fin, opcode) = (first & 0x80 != 0, first & 0x0F);
        if first & 0x70 != 0 {
            return Err(Fault::Protocol("reserved bits set"));
        }
        if second & 0x80 == 0 {
            return Err(Fault::Protocol("client frame not masked"));
        }
        let control = opcode >= CLOSE;
        self.check_opcode(fin, opcode)?;
        let (length, offset) = match second & 0x7F {
            126 if self.buffer.len() >= 4 => (
                usize::from(u16::from_be_bytes([self.buffer[2], self.buffer[3]])),
                4,
            ),
            127 if self.buffer.len() >= 10 => (self.wide_length()?, 10),
            126 | 127 => return Ok(None),
            short => (usize::from(short), 2),
        };
        if control && length > MAX_CONTROL {
            return Err(Fault::Protocol("control frame too long"));
        }
        let held = self
            .partial
            .as_ref()
            .map_or(0, |partial| partial.data.len());
        if !control && held.saturating_add(length) > self.max_message {
            return Err(Fault::TooBig);
        }
        let Some(key) = self.buffer.get(offset..offset + 4) else {
            return Ok(None);
        };
        Ok(Some(Head {
            fin,
            opcode,
            length,
            mask: [key[0], key[1], key[2], key[3]],
            size: offset + 4,
        }))
    }

    fn wide_length(&self) -> Result<usize, Fault> {
        let mut raw = [0; 8];
        raw.copy_from_slice(&self.buffer[2..10]);
        let length = u64::from_be_bytes(raw);
        if length >> 63 != 0 {
            return Err(Fault::Protocol("invalid frame length"));
        }
        usize::try_from(length).map_err(|_| Fault::TooBig)
    }

    fn check_opcode(&self, fin: bool, opcode: u8) -> Result<(), Fault> {
        match opcode {
            CONTINUATION if self.partial.is_none() => {
                Err(Fault::Protocol("continuation without a message"))
            }
            TEXT | BINARY if self.partial.is_some() => {
                Err(Fault::Protocol("new message inside a fragmented one"))
            }
            CLOSE..=PONG if !fin => Err(Fault::Protocol("fragmented control frame")),
            CONTINUATION | TEXT | BINARY | CLOSE..=PONG => Ok(()),
            _ => Err(Fault::Protocol("unknown opcode")),
        }
    }

    fn accept(&mut self, fin: bool, opcode: u8, payload: Vec<u8>) -> Result<Option<Event>, Fault> {
        match opcode {
            PING => Ok(Some(Event::Ping(payload))),
            PONG => Ok(Some(Event::Pong(payload))),
            CLOSE => closing(&payload).map(|closing| Some(Event::Close(closing))),
            _ => self.assemble(fin, opcode == TEXT, payload),
        }
    }

    fn assemble(
        &mut self,
        fin: bool,
        text: bool,
        payload: Vec<u8>,
    ) -> Result<Option<Event>, Fault> {
        let mut message = match self.partial.take() {
            Some(mut partial) => {
                partial.data.extend_from_slice(&payload);
                partial
            }
            None => Partial {
                text,
                data: payload,
            },
        };
        if !fin {
            self.partial = Some(message);
            return Ok(None);
        }
        let data = std::mem::take(&mut message.data);
        if message.text {
            String::from_utf8(data)
                .map(|text| Some(Event::Text(text)))
                .map_err(|_| Fault::NotUtf8)
        } else {
            Ok(Some(Event::Binary(data)))
        }
    }
}

fn closing(payload: &[u8]) -> Result<Option<(u16, String)>, Fault> {
    match payload {
        [] => Ok(None),
        [_] => Err(Fault::Protocol("close frame with one byte")),
        [high, low, reason @ ..] => {
            let code = u16::from_be_bytes([*high, *low]);
            if !sendable_code(code) {
                return Err(Fault::Protocol("invalid close code"));
            }
            let reason = std::str::from_utf8(reason).map_err(|_| Fault::NotUtf8)?;
            Ok(Some((code, reason.to_owned())))
        }
    }
}

fn unmask(payload: &mut [u8], mask: [u8; 4]) {
    let wide = u64::from_ne_bytes([
        mask[0], mask[1], mask[2], mask[3], mask[0], mask[1], mask[2], mask[3],
    ]);
    let (words, rest) = payload.as_chunks_mut::<8>();
    for chunk in words {
        *chunk = (u64::from_ne_bytes(*chunk) ^ wide).to_ne_bytes();
    }
    for (at, byte) in rest.iter_mut().enumerate() {
        *byte ^= mask[at % 4];
    }
}

/// One unfragmented, unmasked frame (what a server sends).
pub fn frame(opcode: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(payload.len() + 10);
    out.push(0x80 | opcode);
    match payload.len() {
        short @ 0..=125 => out.push(short as u8),
        medium @ 126..=0xFFFF => {
            out.push(126);
            out.extend_from_slice(&(medium as u16).to_be_bytes());
        }
        long => {
            out.push(127);
            out.extend_from_slice(&(long as u64).to_be_bytes());
        }
    }
    out.extend_from_slice(payload);
    out
}

/// One frame as a client sends it: masked with `mask`. Used by tests and the
/// fuzz target to build the bytes [`Decoder`] reads.
pub fn masked_frame(opcode: u8, fin: bool, payload: &[u8], mask: [u8; 4]) -> Vec<u8> {
    let plain = frame(opcode, payload);
    let header = plain.len() - payload.len();
    let mut out = Vec::with_capacity(plain.len() + 4);
    out.push(if fin { plain[0] } else { plain[0] & 0x7F });
    out.push(plain[1] | 0x80);
    out.extend_from_slice(&plain[2..header]);
    out.extend_from_slice(&mask);
    out.extend(
        payload
            .iter()
            .enumerate()
            .map(|(at, byte)| byte ^ mask[at % 4]),
    );
    out
}

/// A close frame; a reason is cut at a character boundary to fit the 125 bytes.
pub fn close_frame(code: u16, reason: &str) -> Vec<u8> {
    let mut end = reason.len().min(MAX_CONTROL - 2);
    while !reason.is_char_boundary(end) {
        end -= 1;
    }
    let mut payload = code.to_be_bytes().to_vec();
    payload.extend_from_slice(&reason.as_bytes()[..end]);
    frame(CLOSE, &payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn masked(opcode: u8, fin: bool, payload: &[u8]) -> Vec<u8> {
        let mask = [0x37, 0xFA, 0x21, 0x3D];
        let mut out = vec![if fin { 0x80 } else { 0 } | opcode];
        match payload.len() {
            short @ 0..=125 => out.push(0x80 | short as u8),
            medium @ 126..=0xFFFF => {
                out.push(0x80 | 126);
                out.extend_from_slice(&(medium as u16).to_be_bytes());
            }
            long => {
                out.push(0x80 | 127);
                out.extend_from_slice(&(long as u64).to_be_bytes());
            }
        }
        out.extend_from_slice(&mask);
        out.extend(
            payload
                .iter()
                .enumerate()
                .map(|(at, byte)| byte ^ mask[at % 4]),
        );
        out
    }

    fn decode(bytes: &[u8]) -> Result<Vec<Event>, Fault> {
        let mut decoder = Decoder::new(1024);
        decoder.push(bytes);
        let mut events = Vec::new();
        while let Some(event) = decoder.next()? {
            events.push(event);
        }
        Ok(events)
    }

    fn handshake(extra: &[(&str, &str)]) -> Vec<Header> {
        let mut headers: Vec<(String, String)> = vec![
            ("upgrade".into(), "websocket".into()),
            ("connection".into(), "keep-alive, Upgrade".into()),
            ("sec-websocket-version".into(), "13".into()),
            (
                "sec-websocket-key".into(),
                "dGhlIHNhbXBsZSBub25jZQ==".into(),
            ),
        ];
        headers.extend(extra.iter().map(|(n, v)| ((*n).into(), (*v).into())));
        headers
            .into_iter()
            .map(|(name, value)| (name.into_bytes(), value.into_bytes()))
            .collect()
    }

    #[test]
    fn the_accept_key_is_the_one_of_rfc_6455() {
        assert_eq!(
            accept_key(b"dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }

    #[test]
    fn base64_pads_every_length() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foob"), "Zm9vYg==");
    }

    #[test]
    fn a_handshake_is_read_with_its_subprotocols() {
        let headers = handshake(&[
            ("sec-websocket-protocol", "chat.v1, chat.v2"),
            ("sec-websocket-protocol", "bad value"),
        ]);
        let Upgrade::Offer(offer) = inspect("GET", &headers) else {
            panic!("not an offer");
        };
        assert_eq!(offer.key, "dGhlIHNhbXBsZSBub25jZQ==");
        assert_eq!(offer.protocols, ["chat.v1", "chat.v2"]);
    }

    #[test]
    fn only_a_get_that_asks_for_both_headers_is_an_upgrade() {
        let headers = handshake(&[]);
        assert_eq!(inspect("POST", &headers), Upgrade::Absent);
        let no_connection: Vec<Header> = headers
            .iter()
            .filter(|(name, _)| name != b"connection")
            .cloned()
            .collect();
        assert_eq!(inspect("GET", &no_connection), Upgrade::Absent);
        assert_eq!(inspect("GET", &[]), Upgrade::Absent);
    }

    #[test]
    fn a_wrong_version_or_key_is_named() {
        let version: Vec<Header> = handshake(&[("sec-websocket-version", "8")]);
        assert_eq!(inspect("GET", &version), Upgrade::BadVersion);
        let repeated = handshake(&[("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==")]);
        assert_eq!(inspect("GET", &repeated), Upgrade::BadKey);
        let short: Vec<Header> = handshake(&[])
            .into_iter()
            .map(|(name, value)| {
                if name == b"sec-websocket-key" {
                    (name, b"abc".to_vec())
                } else {
                    (name, value)
                }
            })
            .collect();
        assert_eq!(inspect("GET", &short), Upgrade::BadKey);
    }

    #[test]
    fn text_binary_and_control_frames_decode() {
        let mut bytes = masked(TEXT, true, "héllo".as_bytes());
        bytes.extend(masked(BINARY, true, &[1, 2, 3]));
        bytes.extend(masked(PING, true, b"p"));
        bytes.extend(masked(CLOSE, true, &[0x03, 0xE8, b'o', b'k']));
        assert_eq!(
            decode(&bytes).unwrap(),
            [
                Event::Text("héllo".into()),
                Event::Binary(vec![1, 2, 3]),
                Event::Ping(b"p".to_vec()),
                Event::Close(Some((1000, "ok".into()))),
            ]
        );
    }

    #[test]
    fn long_payloads_use_the_extended_lengths() {
        for size in [126, 1000, 65_535, 65_536] {
            let mut decoder = Decoder::new(100_000);
            decoder.push(&masked(BINARY, true, &vec![7; size]));
            assert_eq!(decoder.next().unwrap(), Some(Event::Binary(vec![7; size])));
        }
    }

    #[test]
    fn a_frame_split_at_any_byte_decodes_the_same() {
        let bytes = masked(TEXT, true, b"split me across reads, please");
        for cut in 0..bytes.len() {
            let mut decoder = Decoder::new(1024);
            decoder.push(&bytes[..cut]);
            assert_eq!(decoder.next().unwrap(), None, "cut {cut}");
            decoder.push(&bytes[cut..]);
            assert!(matches!(decoder.next().unwrap(), Some(Event::Text(_))));
        }
    }

    #[test]
    fn fragments_are_reassembled_around_control_frames() {
        let mut bytes = masked(TEXT, false, b"he");
        bytes.extend(masked(PING, true, b""));
        bytes.extend(masked(CONTINUATION, false, b"l"));
        bytes.extend(masked(CONTINUATION, true, b"lo"));
        assert_eq!(
            decode(&bytes).unwrap(),
            [Event::Ping(Vec::new()), Event::Text("hello".into())]
        );
    }

    #[test]
    fn a_multibyte_character_may_be_split_between_fragments() {
        let bytes = "é".as_bytes();
        let mut stream = masked(TEXT, false, &bytes[..1]);
        stream.extend(masked(CONTINUATION, true, &bytes[1..]));
        assert_eq!(decode(&stream).unwrap(), [Event::Text("é".into())]);
    }

    #[test]
    fn broken_streams_are_refused_with_their_close_code() {
        let refused = |bytes: Vec<u8>| decode(&bytes).unwrap_err();
        let mut unmasked = masked(TEXT, true, b"x");
        unmasked[1] &= 0x7F;
        assert_eq!(refused(unmasked).code(), PROTOCOL);
        let mut reserved = masked(TEXT, true, b"x");
        reserved[0] |= 0x40;
        assert_eq!(refused(reserved).code(), PROTOCOL);
        assert_eq!(refused(masked(0x3, true, b"")).code(), PROTOCOL);
        assert_eq!(refused(masked(CONTINUATION, true, b"x")).code(), PROTOCOL);
        assert_eq!(refused(masked(PING, false, b"")).code(), PROTOCOL);
        assert_eq!(refused(masked(PING, true, &[0; 126])).code(), PROTOCOL);
        assert_eq!(refused(masked(CLOSE, true, &[0x03])).code(), PROTOCOL);
        assert_eq!(refused(masked(CLOSE, true, &[0x03, 0xED])).code(), PROTOCOL);
        assert_eq!(refused(masked(TEXT, true, &[0xFF, 0xFE])).code(), NOT_UTF8);
        assert_eq!(
            refused(masked(CLOSE, true, &[0x03, 0xE8, 0xFF])).code(),
            NOT_UTF8
        );
        let mut nested = masked(TEXT, false, b"a");
        nested.extend(masked(TEXT, true, b"b"));
        assert_eq!(refused(nested).code(), PROTOCOL);
    }

    #[test]
    fn a_too_large_frame_is_refused_before_its_payload_arrives() {
        let mut decoder = Decoder::new(10);
        let mut head = vec![0x82, 0x80 | 126, 0x00, 0x20];
        head.extend_from_slice(&[0; 4]);
        decoder.push(&head);
        assert_eq!(decoder.next(), Err(Fault::TooBig));
    }

    #[test]
    fn the_limit_counts_every_fragment_of_a_message() {
        let mut decoder = Decoder::new(6);
        decoder.push(&masked(BINARY, false, b"1234"));
        assert_eq!(decoder.next(), Ok(None));
        decoder.push(&masked(CONTINUATION, true, b"567"));
        assert_eq!(decoder.next(), Err(Fault::TooBig));
    }

    #[test]
    fn a_length_with_the_top_bit_set_is_refused() {
        let mut decoder = Decoder::new(10);
        let mut head = vec![0x82, 0x80 | 127];
        head.extend_from_slice(&(1u64 << 63).to_be_bytes());
        head.extend_from_slice(&[0; 4]);
        decoder.push(&head);
        assert!(matches!(decoder.next(), Err(Fault::Protocol(_))));
    }

    #[test]
    fn frames_a_server_sends_are_unmasked() {
        assert_eq!(frame(TEXT, b"hi"), [0x81, 2, b'h', b'i']);
        assert_eq!(&frame(BINARY, &[0; 126])[..4], [0x82, 126, 0, 126]);
        assert_eq!(&frame(BINARY, &vec![0; 70_000])[..2], [0x82, 127]);
    }

    #[test]
    fn a_close_reason_is_cut_on_a_character_boundary() {
        let frame = close_frame(1000, &"é".repeat(100));
        assert!(frame.len() <= 2 + MAX_CONTROL);
        assert!(std::str::from_utf8(&frame[4..]).is_ok());
        assert_eq!(&frame[..4], [0x88, (frame.len() - 2) as u8, 0x03, 0xE8]);
    }

    #[test]
    fn unmasking_matches_the_bytewise_rule_at_every_length() {
        let mask = [1, 2, 3, 4];
        for length in 0..40 {
            let plain: Vec<u8> = (0..length as u8).collect();
            let mut bytes = plain.clone();
            unmask(&mut bytes, mask);
            let expected: Vec<u8> = plain
                .iter()
                .enumerate()
                .map(|(at, b)| b ^ mask[at % 4])
                .collect();
            assert_eq!(bytes, expected, "length {length}");
        }
    }
}
