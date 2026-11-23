//! SMTP replies: a bounded reader and parser. Text a server sent is kept only
//! long enough to read the capabilities of an `EHLO` reply.

use tokio::io::{AsyncRead, AsyncReadExt};

/// Longest reply line, with its line break.
pub(crate) const MAX_LINE: usize = 2048;
/// Most lines in one reply.
pub(crate) const MAX_LINES: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Reply {
    pub(crate) code: u16,
    /// The enhanced status code (`5.1.1`) when the first line starts with one.
    pub(crate) enhanced: Option<String>,
    /// The text of each line, without code and line break.
    pub(crate) lines: Vec<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ReadError {
    /// The transport failed.
    Io,
    /// The peer closed the connection.
    Eof,
    /// The reply is malformed or over the limits.
    Protocol,
}

/// One line of a reply: its code, whether more lines follow and its text.
fn parse_line(line: &[u8]) -> Option<(u16, bool, String)> {
    if line.len() < 3 || !line[..3].iter().all(u8::is_ascii_digit) {
        return None;
    }
    let code = u16::from(line[0] - b'0') * 100
        + u16::from(line[1] - b'0') * 10
        + u16::from(line[2] - b'0');
    if !(100..600).contains(&code) {
        return None;
    }
    let more = match line.get(3) {
        None | Some(b' ') => false,
        Some(b'-') => true,
        Some(_) => return None,
    };
    let text = String::from_utf8_lossy(line.get(4..).unwrap_or_default()).into_owned();
    Some((code, more, text))
}

/// `[245].d{1,3}.d{1,3}` at the start of `text`, followed by a blank or the end.
pub(crate) fn enhanced_status(text: &str) -> Option<String> {
    let token = text.split(' ').next()?;
    let mut parts = token.split('.');
    let class = parts.next()?;
    let (subject, detail) = (parts.next()?, parts.next()?);
    let digits =
        |part: &str| (1..=3).contains(&part.len()) && part.bytes().all(|b| b.is_ascii_digit());
    let valid = parts.next().is_none()
        && matches!(class, "2" | "4" | "5")
        && digits(subject)
        && digits(detail);
    valid.then(|| token.to_owned())
}

/// The next line in `pending` without its line break, removed from it.
fn take_line(pending: &mut Vec<u8>) -> Result<Option<Vec<u8>>, ReadError> {
    let Some(end) = pending.iter().position(|b| *b == b'\n') else {
        return if pending.len() >= MAX_LINE {
            Err(ReadError::Protocol)
        } else {
            Ok(None)
        };
    };
    if end + 1 > MAX_LINE {
        return Err(ReadError::Protocol);
    }
    let mut line: Vec<u8> = pending.drain(..=end).collect();
    line.pop();
    if line.last() == Some(&b'\r') {
        line.pop();
    }
    Ok(Some(line))
}

async fn next_line<R: AsyncRead + Unpin>(
    reader: &mut R,
    pending: &mut Vec<u8>,
) -> Result<Vec<u8>, ReadError> {
    loop {
        if let Some(line) = take_line(pending)? {
            return Ok(line);
        }
        let mut chunk = [0u8; 1024];
        match reader.read(&mut chunk).await {
            Ok(0) => return Err(ReadError::Eof),
            Ok(count) => pending.extend_from_slice(&chunk[..count]),
            Err(_) => return Err(ReadError::Io),
        }
    }
}

/// Read one complete reply. `pending` carries bytes read past the end of the
/// previous reply.
pub(crate) async fn read_reply<R: AsyncRead + Unpin>(
    reader: &mut R,
    pending: &mut Vec<u8>,
) -> Result<Reply, ReadError> {
    let mut reply: Option<Reply> = None;
    loop {
        let line = next_line(reader, pending).await?;
        let (code, more, text) = parse_line(&line).ok_or(ReadError::Protocol)?;
        let current = reply.get_or_insert_with(|| Reply {
            code,
            enhanced: enhanced_status(&text),
            lines: Vec::new(),
        });
        if current.code != code || current.lines.len() >= MAX_LINES {
            return Err(ReadError::Protocol);
        }
        current.lines.push(text);
        if !more {
            return reply.ok_or(ReadError::Protocol);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn read(input: &[u8]) -> Result<Reply, ReadError> {
        let mut reader = input;
        read_reply(&mut reader, &mut Vec::new()).await
    }

    #[tokio::test]
    async fn a_single_line_reply_is_parsed() {
        let reply = read(b"250 OK\r\n").await.unwrap();
        assert_eq!(reply.code, 250);
        assert_eq!(reply.lines, ["OK"]);
        assert_eq!(reply.enhanced, None);
    }

    #[tokio::test]
    async fn a_multiline_reply_collects_every_line() {
        let reply = read(b"250-mail.example.invalid\r\n250-STARTTLS\r\n250 SIZE 1000\r\n")
            .await
            .unwrap();
        assert_eq!(
            reply.lines,
            ["mail.example.invalid", "STARTTLS", "SIZE 1000"]
        );
    }

    #[tokio::test]
    async fn bare_line_feeds_and_bare_codes_are_accepted() {
        assert_eq!(read(b"220 hi\n").await.unwrap().code, 220);
        assert_eq!(read(b"354\r\n").await.unwrap().lines, [""]);
    }

    #[tokio::test]
    async fn the_enhanced_status_comes_from_the_first_line() {
        let reply = read(b"550-5.1.1 no such user\r\n550 5.7.1 other\r\n")
            .await
            .unwrap();
        assert_eq!(reply.enhanced.as_deref(), Some("5.1.1"));
    }

    #[test]
    fn enhanced_status_has_a_strict_shape() {
        for good in ["2.0.0 ok", "4.7.1", "5.1.1 x", "5.123.456"] {
            assert!(enhanced_status(good).is_some(), "{good}");
        }
        for bad in [
            "3.0.0", "5.1", "5.1.1.1", "5.1.1x", "5.1234.1", "5.a.1", "", "ok 5.1.1", "5..1",
        ] {
            assert!(enhanced_status(bad).is_none(), "{bad}");
        }
    }

    #[tokio::test]
    async fn malformed_replies_are_protocol_errors() {
        let bad: [&[u8]; 9] = [
            b"abc\r\n",
            b"25\r\n",
            b"250OK\r\n",
            b"099 low\r\n",
            b"600 high\r\n",
            b"250-a\r\n251 b\r\n",
            b"2x0 ok\r\n",
            b"\r\n",
            b"250:ok\r\n",
        ];
        for input in bad {
            assert_eq!(read(input).await, Err(ReadError::Protocol), "{input:?}");
        }
    }

    #[tokio::test]
    async fn line_and_reply_size_are_bounded() {
        let mut line = b"250 ".to_vec();
        line.extend(std::iter::repeat_n(b'x', MAX_LINE));
        line.extend_from_slice(b"\r\n");
        assert_eq!(read(&line).await, Err(ReadError::Protocol));

        let mut unterminated = b"250 ".to_vec();
        unterminated.extend(std::iter::repeat_n(b'x', MAX_LINE + 10));
        assert_eq!(read(&unterminated).await, Err(ReadError::Protocol));

        let mut exact = b"250 ".to_vec();
        exact.extend(std::iter::repeat_n(b'x', MAX_LINE - 6));
        exact.extend_from_slice(b"\r\n");
        assert!(read(&exact).await.is_ok());

        let many = "250-x\r\n".repeat(MAX_LINES);
        let ok = format!("{many}250 end\r\n");
        assert_eq!(read(ok.as_bytes()).await, Err(ReadError::Protocol));
        let ok = format!("{}250 end\r\n", "250-x\r\n".repeat(MAX_LINES - 1));
        assert_eq!(read(ok.as_bytes()).await.unwrap().lines.len(), MAX_LINES);
    }

    #[tokio::test]
    async fn a_closed_stream_is_eof_and_leftover_bytes_are_kept() {
        assert_eq!(read(b"").await, Err(ReadError::Eof));
        assert_eq!(read(b"250-partial\r\n").await, Err(ReadError::Eof));
        let mut reader: &[u8] = b"250 one\r\n354 two\r\n";
        let mut pending = Vec::new();
        assert_eq!(
            read_reply(&mut reader, &mut pending).await.unwrap().code,
            250
        );
        assert_eq!(
            read_reply(&mut reader, &mut pending).await.unwrap().code,
            354
        );
    }

    #[tokio::test]
    async fn a_failing_transport_is_an_io_error() {
        struct Broken;
        impl AsyncRead for Broken {
            fn poll_read(
                self: std::pin::Pin<&mut Self>,
                _: &mut std::task::Context<'_>,
                _: &mut tokio::io::ReadBuf<'_>,
            ) -> std::task::Poll<std::io::Result<()>> {
                std::task::Poll::Ready(Err(std::io::Error::other("reset")))
            }
        }
        let result = read_reply(&mut Broken, &mut Vec::new()).await;
        assert_eq!(result, Err(ReadError::Io));
    }
}
