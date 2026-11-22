//! The request metrics of one worker, in the Prometheus text exposition format.
//!
//! The registry has a fixed layout decided at start from the routes of the plan:
//! one series per (route pattern, method), plus one for the answers no route
//! matched (route `""`). Recording a request is a few relaxed atomic additions
//! and allocates nothing; no label carries a value the client chose, so memory
//! is bounded by the number of routes. The transport records every answer in
//! [`crate::conn::respond`], the place the access log is written, so an early
//! answer, a native one, one from Python and a stream are all counted.

use std::fmt::Write as _;
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Methods with a series of their own; any other method is `OTHER`.
pub const METHODS: [&str; 9] = [
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "QUERY", "OTHER",
];
const OTHER_METHOD: usize = METHODS.len() - 1;
const CLASSES: [&str; 5] = ["1xx", "2xx", "3xx", "4xx", "5xx"];

/// The latency buckets (seconds) used when none are configured.
pub const DEFAULT_BUCKETS: [f64; 11] = [
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];
/// Most buckets a histogram may have.
pub const MAX_BUCKETS: usize = 32;

/// Outcomes of an event handler run, in the order of the `outcome` index.
pub const EVENT_OUTCOMES: [&str; 3] = ["ok", "error", "timeout"];
/// Most event series (event and phase pairs) a registry may have.
pub const MAX_EVENT_SERIES: usize = 4096;

const NO_SLOT: u32 = u32::MAX;

/// Something that went wrong beyond the status of the answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// The backend's answer could not be sent as given; the transport sent a bare 500.
    InvalidResponse,
    /// The producer of a streamed body failed after the head was sent.
    StreamAborted,
}

impl ErrorKind {
    const ALL: [Self; 2] = [Self::InvalidResponse, Self::StreamAborted];

    fn name(self) -> &'static str {
        match self {
            Self::InvalidResponse => "invalid_response",
            Self::StreamAborted => "stream_aborted",
        }
    }
}

/// What a registry needs besides the routes.
#[derive(Debug, Clone)]
pub struct Config {
    /// The `worker` label: the slot the supervisor gave this process.
    pub worker: u32,
    /// The `version` label of `hinoki_build_info`.
    pub version: String,
    /// Upper bounds of the latency histogram, in seconds, strictly increasing.
    pub buckets: Vec<f64>,
    /// The (event, phase) label pairs of the event handlers, known at start. The
    /// index of a pair is the slot [`Registry::observe_event`] takes.
    pub events: Vec<(String, String)>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            worker: 0,
            version: String::new(),
            buckets: DEFAULT_BUCKETS.to_vec(),
            events: Vec::new(),
        }
    }
}

struct EventSeries {
    event: String,
    phase: String,
    outcomes: [AtomicU64; 3],
    /// Handler runs per bucket, not cumulative, whatever the outcome.
    buckets: Box<[AtomicU64]>,
    sum_micros: AtomicU64,
}

impl EventSeries {
    fn new((event, phase): (String, String), buckets: usize) -> Self {
        Self {
            event,
            phase,
            outcomes: Default::default(),
            buckets: (0..=buckets).map(|_| AtomicU64::new(0)).collect(),
            sum_micros: AtomicU64::new(0),
        }
    }
}

struct Series {
    status: [AtomicU64; 5],
    /// Observations per bucket, not cumulative; the last one is `+Inf`.
    buckets: Box<[AtomicU64]>,
    sum_micros: AtomicU64,
}

impl Series {
    fn new(buckets: usize) -> Self {
        Self {
            status: Default::default(),
            buckets: (0..=buckets).map(|_| AtomicU64::new(0)).collect(),
            sum_micros: AtomicU64::new(0),
        }
    }
}

/// The counters and histograms of one worker.
pub struct Registry {
    worker: u32,
    version: String,
    started: u64,
    patterns: Vec<Arc<str>>,
    /// Slot of each action number; `NO_SLOT` for one the plan does not know.
    slots: Vec<u32>,
    bounds: Vec<f64>,
    bounds_micros: Vec<u64>,
    series: Vec<Series>,
    bytes: Vec<AtomicU64>,
    errors: Vec<[AtomicU64; 2]>,
    events: Vec<EventSeries>,
    in_flight: AtomicI64,
    snapshot_errors: AtomicU64,
}

impl std::fmt::Debug for Registry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registry")
            .field("worker", &self.worker)
            .field("routes", &self.patterns.len())
            .finish_non_exhaustive()
    }
}

/// A registry is equal to itself only: a server config holds the one it records into.
impl PartialEq for Registry {
    fn eq(&self, other: &Self) -> bool {
        std::ptr::eq(self, other)
    }
}

impl Eq for Registry {}

/// Holds one request in the in-flight gauge until dropped.
pub struct InFlightGuard(Arc<Registry>);

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::Relaxed);
    }
}

fn check_buckets(buckets: &[f64]) -> Result<(), String> {
    if buckets.is_empty() || buckets.len() > MAX_BUCKETS {
        return Err(format!("buckets must have 1 to {MAX_BUCKETS} entries"));
    }
    let mut last = 0.0;
    for &bound in buckets {
        if !bound.is_finite() || bound <= last {
            return Err("buckets must be finite, positive and strictly increasing".to_owned());
        }
        last = bound;
    }
    Ok(())
}

fn seconds_since_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs())
}

impl Registry {
    /// A registry for `routes` (action number and pattern; several actions may
    /// share one pattern, e.g. `GET` and `DELETE` of `/items/{id}`).
    pub fn new(config: Config, routes: &[(u32, Arc<str>)]) -> Result<Self, String> {
        check_buckets(&config.buckets)?;
        if config.events.len() > MAX_EVENT_SERIES {
            return Err(format!("at most {MAX_EVENT_SERIES} event series"));
        }
        let mut patterns: Vec<Arc<str>> = Vec::new();
        let mut slots = Vec::new();
        for (action, pattern) in routes {
            let slot = patterns
                .iter()
                .position(|known| known == pattern)
                .unwrap_or_else(|| {
                    patterns.push(Arc::clone(pattern));
                    patterns.len() - 1
                });
            let at = *action as usize;
            if slots.len() <= at {
                slots.resize(at + 1, NO_SLOT);
            }
            slots[at] = u32::try_from(slot).map_err(|_| "too many routes".to_owned())?;
        }
        // The last slot is every answer no route matched.
        let count = patterns.len() + 1;
        let buckets = config.buckets.len();
        Ok(Self {
            worker: config.worker,
            version: config.version,
            started: seconds_since_epoch(),
            patterns,
            slots,
            bounds_micros: config.buckets.iter().map(|b| micros_of(*b)).collect(),
            bounds: config.buckets,
            series: (0..count * METHODS.len())
                .map(|_| Series::new(buckets))
                .collect(),
            bytes: (0..count).map(|_| AtomicU64::new(0)).collect(),
            errors: (0..count).map(|_| Default::default()).collect(),
            events: config
                .events
                .into_iter()
                .map(|pair| EventSeries::new(pair, buckets))
                .collect(),
            in_flight: AtomicI64::new(0),
            snapshot_errors: AtomicU64::new(0),
        })
    }

    /// The slot of the route an action belongs to, or the unmatched one.
    pub fn slot_of(&self, action: Option<u32>) -> usize {
        action
            .and_then(|number| self.slots.get(number as usize))
            .filter(|slot| **slot != NO_SLOT)
            .map_or(self.patterns.len(), |slot| *slot as usize)
    }

    /// The index into [`METHODS`] of a request method.
    pub fn method_index(method: &str) -> usize {
        METHODS[..OTHER_METHOD]
            .iter()
            .position(|known| *known == method)
            .unwrap_or(OTHER_METHOD)
    }

    /// Count one request as in flight until the guard is dropped.
    pub fn enter(self: &Arc<Self>) -> InFlightGuard {
        self.in_flight.fetch_add(1, Ordering::Relaxed);
        InFlightGuard(Arc::clone(self))
    }

    /// Record an answered request: its status class and the time to the answer.
    pub fn observe(&self, slot: usize, method: usize, status: u16, elapsed: Duration) {
        let series = &self.series[slot * METHODS.len() + method];
        let class = usize::from((status / 100).clamp(1, 5) - 1);
        series.status[class].fetch_add(1, Ordering::Relaxed);
        let micros = u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX);
        let bucket = self.bounds_micros.partition_point(|bound| *bound < micros);
        series.buckets[bucket].fetch_add(1, Ordering::Relaxed);
        series.sum_micros.fetch_add(micros, Ordering::Relaxed);
    }

    /// Add body bytes handed to the transport for a route.
    pub fn add_bytes(&self, slot: usize, count: usize) {
        self.bytes[slot].fetch_add(count as u64, Ordering::Relaxed);
    }

    pub fn error(&self, slot: usize, kind: ErrorKind) {
        self.errors[slot][kind as usize].fetch_add(1, Ordering::Relaxed);
    }

    /// Record one run of the event handlers of series `slot` (the index of its
    /// pair in [`Config::events`]): `outcome` indexes [`EVENT_OUTCOMES`]. A slot
    /// or outcome out of range is ignored.
    pub fn observe_event(&self, slot: usize, outcome: usize, elapsed: Duration) {
        let Some(series) = self.events.get(slot) else {
            return;
        };
        let Some(count) = series.outcomes.get(outcome) else {
            return;
        };
        count.fetch_add(1, Ordering::Relaxed);
        let micros = u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX);
        let bucket = self.bounds_micros.partition_point(|bound| *bound < micros);
        series.buckets[bucket].fetch_add(1, Ordering::Relaxed);
        series.sum_micros.fetch_add(micros, Ordering::Relaxed);
    }

    /// A peer's snapshot that could not be used.
    pub fn snapshot_error(&self) {
        self.snapshot_errors.fetch_add(1, Ordering::Relaxed);
    }

    fn route(&self, slot: usize) -> &str {
        self.patterns.get(slot).map_or("", |pattern| &**pattern)
    }

    /// The exposition text of this worker; every sample carries `worker`.
    pub fn render(&self) -> String {
        let mut out = String::with_capacity(4096);
        self.render_info(&mut out);
        self.render_requests(&mut out);
        self.render_durations(&mut out);
        family(
            &mut out,
            "hinoki_http_requests_in_flight",
            "gauge",
            "Requests the backend has not answered yet.",
        );
        let _ = writeln!(
            out,
            "hinoki_http_requests_in_flight{{worker=\"{}\"}} {}",
            self.worker,
            self.in_flight.load(Ordering::Relaxed).max(0)
        );
        self.render_bytes(&mut out);
        self.render_errors(&mut out);
        self.render_events(&mut out);
        family(
            &mut out,
            "hinoki_metrics_snapshot_errors_total",
            "counter",
            "Snapshots of other workers that were unreadable, too large or not valid UTF-8.",
        );
        let _ = writeln!(
            out,
            "hinoki_metrics_snapshot_errors_total{{worker=\"{}\"}} {}",
            self.worker,
            self.snapshot_errors.load(Ordering::Relaxed)
        );
        out
    }

    fn render_info(&self, out: &mut String) {
        family(
            out,
            "hinoki_build_info",
            "gauge",
            "The version of Hinoki and the server it runs on.",
        );
        let _ = writeln!(
            out,
            "hinoki_build_info{{version=\"{}\",server=\"kiso\",worker=\"{}\"}} 1",
            escaped(&self.version),
            self.worker
        );
        family(
            out,
            "hinoki_worker_start_time_seconds",
            "gauge",
            "Start time of the worker, in seconds since the Unix epoch.",
        );
        let _ = writeln!(
            out,
            "hinoki_worker_start_time_seconds{{worker=\"{}\"}} {}",
            self.worker, self.started
        );
    }

    fn render_requests(&self, out: &mut String) {
        family(
            out,
            "hinoki_http_requests_total",
            "counter",
            "Requests answered, by route pattern, method and status class.",
        );
        for (index, series) in self.series.iter().enumerate() {
            let route = escaped(self.route(index / METHODS.len()));
            let method = METHODS[index % METHODS.len()];
            for (class, count) in series.status.iter().enumerate() {
                let count = count.load(Ordering::Relaxed);
                if count > 0 {
                    let _ = writeln!(
                        out,
                        "hinoki_http_requests_total{{route=\"{route}\",method=\"{method}\",\
                         status=\"{}\",worker=\"{}\"}} {count}",
                        CLASSES[class], self.worker
                    );
                }
            }
        }
    }

    fn render_durations(&self, out: &mut String) {
        family(
            out,
            "hinoki_http_request_duration_seconds",
            "histogram",
            "Time from the request head to the backend's answer, in seconds.",
        );
        for (index, series) in self.series.iter().enumerate() {
            let route = escaped(self.route(index / METHODS.len()));
            let method = METHODS[index % METHODS.len()];
            self.render_histogram(
                out,
                "hinoki_http_request_duration_seconds",
                &format!("route=\"{route}\",method=\"{method}\""),
                (&series.buckets, &series.sum_micros),
            );
        }
    }

    fn render_events(&self, out: &mut String) {
        if self.events.is_empty() {
            return;
        }
        family(
            out,
            "hinoki_events_total",
            "counter",
            "Event handler runs, by event, phase and outcome.",
        );
        for series in &self.events {
            for (outcome, count) in EVENT_OUTCOMES.iter().zip(&series.outcomes) {
                let count = count.load(Ordering::Relaxed);
                if count > 0 {
                    let _ = writeln!(
                        out,
                        "hinoki_events_total{{event=\"{}\",phase=\"{}\",outcome=\"{outcome}\",\
                         worker=\"{}\"}} {count}",
                        escaped(&series.event),
                        escaped(&series.phase),
                        self.worker
                    );
                }
            }
        }
        family(
            out,
            "hinoki_event_handler_duration_seconds",
            "histogram",
            "Time an event handler ran, in seconds, whatever its outcome.",
        );
        for series in &self.events {
            let labels = format!(
                "event=\"{}\",phase=\"{}\"",
                escaped(&series.event),
                escaped(&series.phase)
            );
            self.render_histogram(
                out,
                "hinoki_event_handler_duration_seconds",
                &labels,
                (&series.buckets, &series.sum_micros),
            );
        }
    }

    /// The bucket, sum and count lines of one histogram series; nothing while
    /// it has no observation.
    fn render_histogram(
        &self,
        out: &mut String,
        name: &str,
        labels: &str,
        (buckets, sum_micros): (&[AtomicU64], &AtomicU64),
    ) {
        let counts: Vec<u64> = buckets
            .iter()
            .map(|bucket| bucket.load(Ordering::Relaxed))
            .collect();
        let total: u64 = counts.iter().sum();
        if total == 0 {
            return;
        }
        let w = self.worker;
        let mut running = 0;
        for (count, bound) in counts.iter().zip(&self.bounds) {
            running += count;
            let _ = writeln!(
                out,
                "{name}_bucket{{{labels},le=\"{bound}\",worker=\"{w}\"}} {running}"
            );
        }
        let sum = sum_micros.load(Ordering::Relaxed) as f64 / 1e6;
        let _ = writeln!(
            out,
            "{name}_bucket{{{labels},le=\"+Inf\",worker=\"{w}\"}} {total}\n\
             {name}_sum{{{labels},worker=\"{w}\"}} {sum}\n\
             {name}_count{{{labels},worker=\"{w}\"}} {total}"
        );
    }

    fn render_bytes(&self, out: &mut String) {
        family(
            out,
            "hinoki_http_response_bytes_total",
            "counter",
            "Response body bytes handed to the connection, after compression.",
        );
        for (slot, count) in self.bytes.iter().enumerate() {
            let count = count.load(Ordering::Relaxed);
            if count > 0 {
                let _ = writeln!(
                    out,
                    "hinoki_http_response_bytes_total{{route=\"{}\",worker=\"{}\"}} {count}",
                    escaped(self.route(slot)),
                    self.worker
                );
            }
        }
    }

    fn render_errors(&self, out: &mut String) {
        family(
            out,
            "hinoki_http_errors_total",
            "counter",
            "Answers the transport could not send as given and streams that failed.",
        );
        for (slot, counts) in self.errors.iter().enumerate() {
            for kind in ErrorKind::ALL {
                let count = counts[kind as usize].load(Ordering::Relaxed);
                if count > 0 {
                    let _ = writeln!(
                        out,
                        "hinoki_http_errors_total{{route=\"{}\",kind=\"{}\",worker=\"{}\"}} {count}",
                        escaped(self.route(slot)),
                        kind.name(),
                        self.worker
                    );
                }
            }
        }
    }
}

fn micros_of(seconds: f64) -> u64 {
    (seconds * 1e6).round() as u64
}

fn family(out: &mut String, name: &str, kind: &str, help: &str) {
    let _ = write!(out, "# HELP {name} {help}\n# TYPE {name} {kind}\n");
}

/// A label value: backslash, quote and line feed are escaped.
fn escaped(value: &str) -> std::borrow::Cow<'_, str> {
    if !value.contains(['\\', '"', '\n']) {
        return value.into();
    }
    let mut out = String::with_capacity(value.len() + 2);
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            c => out.push(c),
        }
    }
    out.into()
}

/// Several workers' expositions as one: the samples of a family are put
/// together under one `# HELP` and `# TYPE`, as the format requires.
pub fn merge(parts: &[&str]) -> String {
    let mut merged = Merged::default();
    for text in parts {
        merged.absorb(text);
    }
    merged.render()
}

type Family<'a> = (Vec<&'a str>, Vec<&'a str>);

#[derive(Default)]
struct Merged<'a> {
    order: Vec<&'a str>,
    families: std::collections::HashMap<&'a str, Family<'a>>,
}

impl<'a> Merged<'a> {
    fn absorb(&mut self, text: &'a str) {
        let mut current: Option<&'a str> = None;
        let mut fresh = false;
        for line in text.lines().filter(|line| !line.is_empty()) {
            if let Some(rest) = line.strip_prefix("# HELP ") {
                let name = rest.split(' ').next().unwrap_or(rest);
                fresh = self.open(name);
                current = Some(name);
            }
            if let Some(entry) = current.and_then(|name| self.families.get_mut(name)) {
                Self::file(entry, line, fresh);
            }
        }
    }

    /// Registers a family on first sight; true when it is new.
    fn open(&mut self, name: &'a str) -> bool {
        let fresh = !self.families.contains_key(name);
        if fresh {
            self.order.push(name);
            self.families.insert(name, (Vec::new(), Vec::new()));
        }
        fresh
    }

    fn file(entry: &mut Family<'a>, line: &'a str, fresh: bool) {
        if !line.starts_with('#') {
            entry.1.push(line);
        } else if fresh {
            entry.0.push(line);
        }
    }

    fn render(&self) -> String {
        let mut out = String::new();
        for (head, samples) in self.order.iter().filter_map(|name| self.families.get(name)) {
            for line in head.iter().chain(samples) {
                out.push_str(line);
                out.push('\n');
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry() -> Arc<Registry> {
        let routes = [
            (0, Arc::<str>::from("/items/{id}")),
            (1, Arc::<str>::from("/items/{id}")),
            (2, Arc::<str>::from("/items")),
        ];
        let config = Config {
            worker: 3,
            version: "0.0.1".to_owned(),
            buckets: vec![0.01, 0.1, 1.0],
            events: vec![
                ("Created[Note]".to_owned(), "after_commit".to_owned()),
                ("Noted".to_owned(), "in_transaction".to_owned()),
            ],
        };
        Arc::new(Registry::new(config, &routes).unwrap())
    }

    #[test]
    fn event_runs_are_counted_by_outcome_and_timed() {
        let registry = registry();
        registry.observe_event(0, 0, Duration::from_millis(5));
        registry.observe_event(0, 0, Duration::from_millis(50));
        registry.observe_event(0, 2, Duration::from_secs(30));
        registry.observe_event(1, 1, Duration::from_millis(5));
        registry.observe_event(7, 0, Duration::ZERO);
        registry.observe_event(0, 9, Duration::ZERO);
        let text = registry.render();
        let total = |event: &str, phase: &str, outcome: &str, count: u32| {
            format!(
                "hinoki_events_total{{event=\"{event}\",phase=\"{phase}\",\
                 outcome=\"{outcome}\",worker=\"3\"}} {count}"
            )
        };
        assert!(text.contains(&total("Created[Note]", "after_commit", "ok", 2)));
        assert!(text.contains(&total("Created[Note]", "after_commit", "timeout", 1)));
        assert!(text.contains(&total("Noted", "in_transaction", "error", 1)));
        assert!(!text.contains("outcome=\"error\",worker=\"3\"} 2"));
        let labels = "event=\"Created[Note]\",phase=\"after_commit\"";
        assert!(text.contains(&format!(
            "hinoki_event_handler_duration_seconds_bucket{{{labels},le=\"0.01\",worker=\"3\"}} 1"
        )));
        assert!(text.contains(&format!(
            "hinoki_event_handler_duration_seconds_bucket{{{labels},le=\"+Inf\",worker=\"3\"}} 3"
        )));
        assert!(text.contains(&format!(
            "hinoki_event_handler_duration_seconds_sum{{{labels},worker=\"3\"}} 30.055"
        )));
    }

    #[test]
    fn no_event_family_without_registered_events() {
        let text = Registry::new(Config::default(), &[]).unwrap().render();
        assert!(!text.contains("hinoki_event"));
    }

    #[test]
    fn too_many_event_series_are_refused() {
        let config = Config {
            events: vec![(String::new(), String::new()); MAX_EVENT_SERIES + 1],
            ..Config::default()
        };
        assert!(Registry::new(config, &[]).is_err());
    }

    #[test]
    fn actions_of_one_pattern_share_a_slot() {
        let registry = registry();
        assert_eq!(registry.slot_of(Some(0)), registry.slot_of(Some(1)));
        assert_ne!(registry.slot_of(Some(0)), registry.slot_of(Some(2)));
        let unmatched = registry.slot_of(None);
        assert_eq!(registry.slot_of(Some(99)), unmatched);
        assert_eq!(registry.route(unmatched), "");
    }

    #[test]
    fn a_request_lands_in_its_status_class_and_bucket() {
        let registry = registry();
        let slot = registry.slot_of(Some(0));
        let get = Registry::method_index("GET");
        registry.observe(slot, get, 200, Duration::from_millis(5));
        registry.observe(slot, get, 404, Duration::from_millis(50));
        registry.observe(slot, get, 200, Duration::from_secs(30));
        let text = registry.render();
        let requests = |class: &str, count: u32| {
            format!(
                "hinoki_http_requests_total{{route=\"/items/{{id}}\",method=\"GET\",\
                 status=\"{class}\",worker=\"3\"}} {count}"
            )
        };
        assert!(text.contains(&requests("2xx", 2)));
        assert!(text.contains(&requests("4xx", 1)));
        let bucket = |le: &str, count: u32| {
            format!(
                "hinoki_http_request_duration_seconds_bucket{{route=\"/items/{{id}}\",\
                 method=\"GET\",le=\"{le}\",worker=\"3\"}} {count}"
            )
        };
        assert!(text.contains(&bucket("0.01", 1)));
        assert!(text.contains(&bucket("0.1", 2)));
        assert!(text.contains(&bucket("1", 2)));
        assert!(text.contains(&bucket("+Inf", 3)));
        assert!(text.contains("_count{route=\"/items/{id}\",method=\"GET\",worker=\"3\"} 3"));
        assert!(text.contains("_sum{route=\"/items/{id}\",method=\"GET\",worker=\"3\"} 30.055"));
    }

    #[test]
    fn a_bound_is_inclusive_and_other_methods_share_one_series() {
        let registry = registry();
        let slot = registry.slot_of(None);
        registry.observe(
            slot,
            Registry::method_index("BREW"),
            503,
            Duration::from_millis(10),
        );
        let text = registry.render();
        assert!(text.contains("route=\"\",method=\"OTHER\",status=\"5xx\",worker=\"3\"} 1"));
        assert!(text.contains("method=\"OTHER\",le=\"0.01\",worker=\"3\"} 1"));
    }

    #[test]
    fn in_flight_follows_the_guard() {
        let registry = registry();
        let first = registry.enter();
        let second = registry.enter();
        assert!(registry.render().contains("in_flight{worker=\"3\"} 2"));
        drop(first);
        drop(second);
        assert!(registry.render().contains("in_flight{worker=\"3\"} 0"));
    }

    #[test]
    fn bytes_and_errors_are_counted_per_route() {
        let registry = registry();
        let slot = registry.slot_of(Some(2));
        registry.add_bytes(slot, 10);
        registry.add_bytes(slot, 5);
        registry.error(slot, ErrorKind::StreamAborted);
        let text = registry.render();
        assert!(text.contains("response_bytes_total{route=\"/items\",worker=\"3\"} 15"));
        assert!(
            text.contains("errors_total{route=\"/items\",kind=\"stream_aborted\",worker=\"3\"} 1")
        );
        assert!(!text.contains("invalid_response"));
    }

    #[test]
    fn label_values_are_escaped() {
        assert_eq!(escaped("a\"b\\c\nd"), "a\\\"b\\\\c\\nd");
    }

    #[test]
    fn buckets_must_increase() {
        let routes: [(u32, Arc<str>); 0] = [];
        for bad in [vec![], vec![0.1, 0.1], vec![0.0, 1.0], vec![f64::NAN]] {
            let config = Config {
                buckets: bad,
                ..Config::default()
            };
            assert!(Registry::new(config, &routes).is_err());
        }
    }

    #[test]
    fn merge_groups_the_samples_of_a_family() {
        let first = "# HELP a One.\n# TYPE a counter\na{worker=\"0\"} 1\n# HELP b Two.\n# TYPE b gauge\nb{worker=\"0\"} 1\n";
        let second = "# HELP a One.\n# TYPE a counter\na{worker=\"1\"} 2\n# HELP b Two.\n# TYPE b gauge\nb{worker=\"1\"} 2\n";
        assert_eq!(
            merge(&[first, second]),
            "# HELP a One.\n# TYPE a counter\na{worker=\"0\"} 1\na{worker=\"1\"} 2\n\
             # HELP b Two.\n# TYPE b gauge\nb{worker=\"0\"} 1\nb{worker=\"1\"} 2\n"
        );
    }
}
