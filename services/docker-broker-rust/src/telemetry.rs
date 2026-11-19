//! Payload-free HTTP traces, metrics and correlated logs. No raw URL, header,
//! body, error detail or Docker response is exported.
use std::{
    collections::HashMap,
    env,
    pin::Pin,
    sync::Arc,
    task::{Context as TaskContext, Poll},
    time::{Duration, Instant, SystemTime},
};

use axum::{
    body::{Body, Bytes},
    extract::{MatchedPath, Request},
    http::{HeaderMap, Method, StatusCode},
    middleware::Next,
    response::Response,
};
use hyper::body::{Body as HttpBody, Frame, SizeHint};
use opentelemetry::{
    Context, KeyValue,
    logs::{LogRecord, Logger as _, LoggerProvider, Severity},
    metrics::{Histogram, MeterProvider},
    propagation::{Extractor, Injector, TextMapPropagator},
    trace::{SpanKind, Status, TraceContextExt, Tracer as _, TracerProvider},
};
use opentelemetry_otlp::{WithExportConfig, WithHttpConfig};
use opentelemetry_sdk::{
    Resource,
    logs::{SdkLogger, SdkLoggerProvider},
    metrics::{PeriodicReader, SdkMeterProvider},
    propagation::TraceContextPropagator,
    trace::{BatchConfigBuilder, BatchSpanProcessor, SdkTracer, SdkTracerProvider},
};

const SERVICE: &str = "nautionette-docker-broker";
const TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug)]
pub struct TelemetryError;

pub struct Otlp {
    tracer: SdkTracer,
    traces: SdkTracerProvider,
    meters: SdkMeterProvider,
    logs: SdkLoggerProvider,
    requests: Histogram<f64>,
    logger: SdkLogger,
}

impl Otlp {
    /// Call from a blocking thread: the SDK's HTTP exporter uses blocking I/O
    /// on its own batch-worker threads, not on the Axum request runtime.
    pub fn from_env() -> Result<Option<Arc<Self>>, TelemetryError> {
        let endpoint = env::var("OTEL_EXPORTER_OTLP_ENDPOINT").unwrap_or_default();
        if endpoint.is_empty()
            || env::var("OTEL_SDK_DISABLED").is_ok_and(|v| v.eq_ignore_ascii_case("true"))
        {
            return Ok(None);
        }
        Self::new(
            &endpoint,
            &env::var("APP_VERSION").unwrap_or_else(|_| "dev".to_owned()),
            &env::var("APP_ENVIRONMENT").unwrap_or_else(|_| "production".to_owned()),
            HashMap::new(),
        )
        .map(Some)
    }

    pub fn new(
        endpoint: &str,
        version: &str,
        environment: &str,
        headers: HashMap<String, String>,
    ) -> Result<Arc<Self>, TelemetryError> {
        let url = reqwest::Url::parse(endpoint).map_err(|_| TelemetryError)?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(TelemetryError);
        }
        let endpoint = endpoint.trim_end_matches('/');
        let client = reqwest::blocking::Client::builder()
            .timeout(TIMEOUT)
            .build()
            .map_err(|_| TelemetryError)?;
        let resource = Resource::builder_empty()
            .with_attributes([
                KeyValue::new("service.name", SERVICE),
                KeyValue::new("service.namespace", "nautionette"),
                KeyValue::new("service.version", version.to_owned()),
                KeyValue::new("deployment.environment", environment.to_owned()),
                KeyValue::new("deployment.environment.name", environment.to_owned()),
            ])
            .build();
        let trace = opentelemetry_otlp::SpanExporter::builder()
            .with_http()
            .with_endpoint(format!("{endpoint}/v1/traces"))
            .with_timeout(TIMEOUT)
            .with_headers(headers.clone())
            .with_http_client(client.clone())
            .build()
            .map_err(|_| TelemetryError)?;
        let metric = opentelemetry_otlp::MetricExporter::builder()
            .with_http()
            .with_endpoint(format!("{endpoint}/v1/metrics"))
            .with_timeout(TIMEOUT)
            .with_headers(headers.clone())
            .with_http_client(client.clone())
            .build()
            .map_err(|_| TelemetryError)?;
        let log = opentelemetry_otlp::LogExporter::builder()
            .with_http()
            .with_endpoint(format!("{endpoint}/v1/logs"))
            .with_timeout(TIMEOUT)
            .with_headers(headers)
            .with_http_client(client)
            .build()
            .map_err(|_| TelemetryError)?;
        let batch = BatchSpanProcessor::builder(trace)
            .with_batch_config(
                BatchConfigBuilder::default()
                    .with_max_queue_size(2048)
                    .with_max_export_batch_size(256)
                    .build(),
            )
            .build();
        let traces = SdkTracerProvider::builder()
            .with_resource(resource.clone())
            .with_span_processor(batch)
            .build();
        let meters = SdkMeterProvider::builder()
            .with_resource(resource.clone())
            .with_reader(
                PeriodicReader::builder(metric)
                    .with_interval(Duration::from_secs(30))
                    .build(),
            )
            .build();
        let logs = SdkLoggerProvider::builder()
            .with_resource(resource)
            .with_batch_exporter(log)
            .build();
        let requests = meters
            .meter(SERVICE)
            .f64_histogram("http.server.request.duration")
            .with_unit("s")
            .build();
        let tracer = traces.tracer(SERVICE);
        let logger = logs.logger(SERVICE);
        Ok(Arc::new(Self {
            tracer,
            traces,
            meters,
            logs,
            requests,
            logger,
        }))
    }

    /// Useful for conformance tests and explicit shutdown, not request handling.
    pub fn flush(&self) {
        let _ = self.traces.force_flush();
        let _ = self.meters.force_flush();
        let _ = self.logs.force_flush();
    }

    pub(crate) fn shutdown(&self) {
        self.flush();
        let _ = self.traces.shutdown();
        let _ = self.meters.shutdown();
        let _ = self.logs.shutdown();
    }
}

struct Inbound<'a>(&'a HeaderMap);
impl Extractor for Inbound<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key)?.to_str().ok()
    }
    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(|name| name.as_str()).collect()
    }
}
#[derive(Default, Clone)]
pub(crate) struct TraceCarrier {
    pub(crate) traceparent: Option<String>,
}
impl Injector for TraceCarrier {
    fn set(&mut self, key: &str, value: String) {
        if key == "traceparent" {
            self.traceparent = Some(value);
        }
    }
}

pub(crate) async fn instrument(otel: &Arc<Otlp>, mut request: Request, next: Next) -> Response {
    let method = match *request.method() {
        Method::GET => "GET",
        Method::POST => "POST",
        Method::HEAD => "HEAD",
        Method::OPTIONS => "OPTIONS",
        _ => "_OTHER",
    };
    let route = request
        .extensions()
        .get::<MatchedPath>()
        .map(|path| path.as_str().to_owned());
    let duration = Instant::now();
    let propagator = TraceContextPropagator::new();
    let parent = propagator.extract(&Inbound(request.headers()));
    let mut attrs = vec![KeyValue::new("http.request.method", method)];
    if let Some(route) = &route {
        attrs.push(KeyValue::new("http.route", route.clone()));
    }
    let span = otel
        .tracer
        .span_builder(format!(
            "{method} {}",
            route.as_deref().unwrap_or("request")
        ))
        .with_kind(SpanKind::Server)
        .with_attributes(attrs.clone())
        .start_with_context(&otel.tracer, &parent);
    let context = parent.with_span(span);
    let mut carrier = TraceCarrier::default();
    propagator.inject_context(&context, &mut carrier);
    request.extensions_mut().insert(carrier);
    let response = next.run(request).await;
    let (parts, body) = response.into_parts();
    let status = parts.status;
    attrs.push(KeyValue::new(
        "http.response.status_code",
        i64::from(status.as_u16()),
    ));
    context
        .span()
        .set_attribute(attrs.last().expect("status attached").clone());
    if status.is_server_error() {
        context.span().set_status(Status::error(""));
    }
    let observed = ObserveBody {
        inner: Box::pin(body),
        observation: Some(Observation {
            otel: otel.clone(),
            context,
            attrs,
            status,
            duration,
        }),
    };
    Response::from_parts(parts, Body::new(observed))
}

struct Observation {
    otel: Arc<Otlp>,
    context: Context,
    attrs: Vec<KeyValue>,
    status: StatusCode,
    duration: Instant,
}
impl Drop for Observation {
    fn drop(&mut self) {
        let span = self.context.span();
        span.end();
        self.otel
            .requests
            .record(self.duration.elapsed().as_secs_f64(), &self.attrs);
        let mut record = self.otel.logger.create_log_record();
        record.set_timestamp(SystemTime::now());
        let (severity, label) = if self.status.is_server_error() {
            (Severity::Error, "ERROR")
        } else {
            (Severity::Info, "INFO")
        };
        record.set_severity_number(severity);
        record.set_severity_text(label);
        record.set_body(format!("{SERVICE} {label}").into());
        let context = span.span_context();
        record.set_trace_context(
            context.trace_id(),
            context.span_id(),
            Some(context.trace_flags()),
        );
        self.otel.logger.emit(record);
    }
}
struct ObserveBody {
    inner: Pin<Box<Body>>,
    observation: Option<Observation>,
}
impl HttpBody for ObserveBody {
    type Data = Bytes;
    type Error = axum::Error;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        let result = self.inner.as_mut().poll_frame(cx);
        if matches!(result, Poll::Ready(None | Some(Err(_)))) {
            self.observation.take();
        }
        result
    }
    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }
    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}
