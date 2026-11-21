//! The encoders behind response compression and the body that feeds one.
//!
//! [`Encoded`] pulls pieces of at most [`FILE_PIECE`] bytes from a source (bytes
//! in memory, a [`Live`] producer or a [`FileBody`]), writes them to a
//! synchronous encoder and hands out what the encoder produced. The encoder
//! works on one piece per step, so a large body never holds the runtime thread
//! for longer than one piece takes; after a few steps without output the body
//! yields. When the source has nothing ready the encoder is flushed, so a piece
//! never waits in the encoder while the producer is quiet. A source that fails
//! ends the body as [`Aborted`] without the closing bytes of the format, so the
//! client never reads a failed body as complete.

use std::io::Write;
use std::task::{Context, Poll};

use brotli::CompressorWriter;
use bytes::Bytes;
use flate2::write::GzEncoder;
use tokio::sync::OwnedSemaphorePermit;

use crate::stream::{report, Aborted, FileBody, Live, FILE_PIECE};

/// A content coding Kiso can produce.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Coding {
    Brotli,
    Gzip,
}

impl Coding {
    /// The token of the `content-encoding` header.
    pub const fn token(self) -> &'static str {
        match self {
            Self::Brotli => "br",
            Self::Gzip => "gzip",
        }
    }
}

/// How hard to compress.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Levels {
    pub gzip: u32,
    pub brotli_quality: u32,
    pub brotli_window: u32,
}

/// Source pieces an [`Encoded`] body takes in one `poll` before it yields.
const STEPS: usize = 4;
/// Size of brotli's internal input buffer.
const BROTLI_BUFFER: usize = 4096;

enum Encoder {
    Gzip(GzEncoder<Vec<u8>>),
    Brotli(Box<CompressorWriter<Vec<u8>>>),
}

impl Encoder {
    fn new(coding: Coding, levels: Levels) -> Self {
        match coding {
            Coding::Gzip => Self::Gzip(GzEncoder::new(
                Vec::new(),
                flate2::Compression::new(levels.gzip),
            )),
            Coding::Brotli => Self::Brotli(Box::new(CompressorWriter::new(
                Vec::new(),
                BROTLI_BUFFER,
                levels.brotli_quality,
                levels.brotli_window,
            ))),
        }
    }

    fn write(&mut self, data: &[u8]) -> std::io::Result<()> {
        match self {
            Self::Gzip(encoder) => encoder.write_all(data),
            Self::Brotli(encoder) => encoder.write_all(data),
        }
    }

    /// Emit everything written so far, so a decoder can read all of it.
    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            Self::Gzip(encoder) => encoder.flush(),
            Self::Brotli(encoder) => encoder.flush(),
        }
    }

    /// The output produced since the last call.
    fn take(&mut self) -> Bytes {
        let buffer = match self {
            Self::Gzip(encoder) => encoder.get_mut(),
            Self::Brotli(encoder) => encoder.get_mut(),
        };
        Bytes::from(std::mem::take(buffer))
    }

    fn finish(self) -> std::io::Result<Bytes> {
        Ok(Bytes::from(match self {
            Self::Gzip(encoder) => encoder.finish()?,
            Self::Brotli(encoder) => encoder.into_inner(),
        }))
    }
}

/// Compress `data` in one go; `None` when the encoder fails.
pub fn encode_all(coding: Coding, levels: Levels, data: &[u8]) -> Option<Bytes> {
    let mut encoder = Encoder::new(coding, levels);
    encoder.write(data).ok()?;
    encoder.finish().ok()
}

/// Where an [`Encoded`] body takes its bytes from.
pub enum Source {
    Memory(Bytes),
    Live(Live),
    File(FileBody),
}

impl Source {
    fn poll_piece(
        &mut self,
        cx: &mut Context<'_>,
        limit: usize,
    ) -> Poll<Option<Result<Bytes, Aborted>>> {
        match self {
            Self::Memory(bytes) if bytes.is_empty() => Poll::Ready(None),
            Self::Memory(bytes) => {
                let take = bytes.len().min(limit);
                Poll::Ready(Some(Ok(bytes.split_to(take))))
            }
            Self::Live(live) => live.poll_piece(cx, limit),
            Self::File(file) => file.poll_piece(cx, limit),
        }
    }
}

/// A body whose bytes are the encoding of a [`Source`].
pub struct Encoded {
    source: Source,
    encoder: Option<Encoder>,
    pending: Bytes,
    unflushed: bool,
    finished: bool,
    /// One of the compressions the policy allows at once; given back when the
    /// body ends or fails.
    slot: Option<OwnedSemaphorePermit>,
}

impl Encoded {
    pub fn new(source: Source, coding: Coding, levels: Levels, slot: OwnedSemaphorePermit) -> Self {
        Self {
            source,
            encoder: Some(Encoder::new(coding, levels)),
            pending: Bytes::new(),
            unflushed: false,
            finished: false,
            slot: Some(slot),
        }
    }

    /// The next piece of at most `limit` bytes, the source's failure, or the end.
    pub fn poll_piece(
        &mut self,
        cx: &mut Context<'_>,
        limit: usize,
    ) -> Poll<Option<Result<Bytes, Aborted>>> {
        for _ in 0..STEPS {
            if !self.pending.is_empty() {
                let take = self.pending.len().min(limit);
                return Poll::Ready(Some(Ok(self.pending.split_to(take))));
            }
            if self.finished {
                return Poll::Ready(None);
            }
            match self.advance(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Some(aborted)) => return Poll::Ready(Some(Err(aborted))),
                Poll::Ready(None) => {}
            }
        }
        cx.waker().wake_by_ref();
        Poll::Pending
    }

    /// Whether the body ended and nothing is left to send.
    pub fn is_done(&self) -> bool {
        self.finished && self.pending.is_empty()
    }

    /// Take one step: `Ready(None)` says the state changed and the caller looks
    /// again, `Ready(Some)` is a failure to report.
    fn advance(&mut self, cx: &mut Context<'_>) -> Poll<Option<Aborted>> {
        match self.source.poll_piece(cx, FILE_PIECE) {
            Poll::Pending => self.flush_idle(),
            Poll::Ready(Some(Ok(piece))) => self.feed(&piece),
            Poll::Ready(Some(Err(aborted))) => {
                self.stop();
                Poll::Ready(Some(aborted))
            }
            Poll::Ready(None) => self.finish(),
        }
    }

    fn stop(&mut self) {
        self.finished = true;
        self.encoder = None;
        self.slot = None;
    }

    fn fail(&mut self, error: &std::io::Error) -> Poll<Option<Aborted>> {
        report(&format!("the response cannot be compressed ({error})"));
        self.stop();
        Poll::Ready(Some(Aborted))
    }

    fn feed(&mut self, piece: &[u8]) -> Poll<Option<Aborted>> {
        let Some(encoder) = self.encoder.as_mut() else {
            return Poll::Ready(None);
        };
        if let Err(error) = encoder.write(piece) {
            return self.fail(&error);
        }
        self.unflushed = true;
        self.pending = encoder.take();
        Poll::Ready(None)
    }

    /// The source has nothing ready: send what the encoder holds, or wait.
    fn flush_idle(&mut self) -> Poll<Option<Aborted>> {
        let Some(encoder) = self.encoder.as_mut().filter(|_| self.unflushed) else {
            return Poll::Pending;
        };
        if let Err(error) = encoder.flush() {
            return self.fail(&error);
        }
        self.unflushed = false;
        self.pending = encoder.take();
        Poll::Ready(None)
    }

    fn finish(&mut self) -> Poll<Option<Aborted>> {
        let Some(encoder) = self.encoder.take() else {
            self.finished = true;
            return Poll::Ready(None);
        };
        match encoder.finish() {
            Ok(tail) => {
                self.pending = tail;
                self.stop();
                Poll::Ready(None)
            }
            Err(error) => self.fail(&error),
        }
    }
}
