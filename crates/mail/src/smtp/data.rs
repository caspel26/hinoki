//! The `DATA` payload writer: dot-stuffing and the end marker.

/// Doubles a `.` at the start of a line, chunk by chunk.
pub(crate) struct DotStuffer {
    at_start: bool,
    after_cr: bool,
}

impl DotStuffer {
    pub(crate) fn new() -> Self {
        Self {
            at_start: true,
            after_cr: false,
        }
    }

    pub(crate) fn push(&mut self, chunk: &[u8], out: &mut Vec<u8>) {
        for &byte in chunk {
            if self.at_start && byte == b'.' {
                out.push(b'.');
            }
            out.push(byte);
            self.at_start = self.after_cr && byte == b'\n';
            self.after_cr = byte == b'\r';
        }
    }

    /// End the payload: complete the last line if needed, then `.` CRLF.
    pub(crate) fn finish(self, out: &mut Vec<u8>) {
        if !self.at_start {
            out.extend_from_slice(b"\r\n");
        }
        out.extend_from_slice(b".\r\n");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stuff(chunks: &[&[u8]]) -> Vec<u8> {
        let mut stuffer = DotStuffer::new();
        let mut out = Vec::new();
        for chunk in chunks {
            stuffer.push(chunk, &mut out);
        }
        stuffer.finish(&mut out);
        out
    }

    #[test]
    fn leading_dots_are_doubled_and_the_marker_added() {
        assert_eq!(
            stuff(&[b".hidden\r\na.b\r\n.\r\n..x\r\n"]),
            b"..hidden\r\na.b\r\n..\r\n...x\r\n.\r\n"
        );
    }

    #[test]
    fn a_missing_final_line_break_is_added() {
        assert_eq!(stuff(&[b"no break"]), b"no break\r\n.\r\n");
        assert_eq!(stuff(&[b""]), b".\r\n");
        assert_eq!(stuff(&[b"line\r\n"]), b"line\r\n.\r\n");
    }

    #[test]
    fn chunk_boundaries_do_not_change_the_output() {
        let whole = b"a\r\n.b\r\n..c\r\n.\r\nend";
        let expected = stuff(&[whole]);
        for split in 0..=whole.len() {
            let (left, right) = whole.split_at(split);
            assert_eq!(stuff(&[left, right]), expected, "split at {split}");
        }
    }

    #[test]
    fn a_dot_after_a_lone_line_feed_is_not_a_line_start() {
        assert_eq!(stuff(&[b"a\n.b\r\n"]), b"a\n.b\r\n.\r\n");
    }
}
