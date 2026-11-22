#![no_main]

use kiso::ws::{Decoder, Event, Fault};
use libfuzzer_sys::fuzz_target;

const MAX_MESSAGE: usize = 4096;

fn events(bytes: &[u8], chunk: usize) -> (Vec<Event>, Option<Fault>) {
    let mut decoder = Decoder::new(MAX_MESSAGE);
    let mut found = Vec::new();
    for piece in bytes.chunks(chunk) {
        decoder.push(piece);
        loop {
            match decoder.next() {
                Ok(Some(event)) => found.push(event),
                Ok(None) => break,
                Err(fault) => return (found, Some(fault)),
            }
        }
    }
    (found, None)
}

// The frame decoder reads whatever a client sends. However the bytes are cut
// into reads it must not panic, must give the same events, and must never hand
// out a message above the limit.
fuzz_target!(|input: &[u8]| {
    let Some((&cut, bytes)) = input.split_first() else {
        return;
    };
    let whole = events(bytes, bytes.len().max(1));
    let split = events(bytes, usize::from(cut).max(1));
    assert_eq!(whole, split);
    for event in &whole.0 {
        let size = match event {
            Event::Text(text) => text.len(),
            Event::Binary(data) => data.len(),
            _ => 0,
        };
        assert!(size <= MAX_MESSAGE);
    }
});
