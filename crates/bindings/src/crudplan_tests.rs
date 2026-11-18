use super::{
    parse_model_default, validate_error_template, validate_metadata, ModelDefaultValue,
    REQUEST_ID_SENTINEL,
};

#[test]
fn validation_template_requires_the_framework_error_shape() {
    let valid = format!(
        r#"{{"error":"validation_error","request_id":"{REQUEST_ID_SENTINEL}","details":[{{"path":"data.name","code":"type","message":"invalid"}}]}}"#
    );
    assert!(validate_error_template(valid.as_bytes()).is_ok());
    assert!(validate_error_template(b"{}").is_err());
}

#[test]
fn metadata_rejects_empty_or_duplicate_column_names() {
    let no_defaults: Vec<(String, String, String)> = Vec::new();
    let no_columns: Vec<String> = Vec::new();
    assert!(validate_metadata(
        "list",
        &["id".to_owned()],
        "id",
        20,
        100,
        &[("id".to_owned(), "uuid".to_owned(), false)],
        &no_defaults,
        &no_defaults,
        &no_defaults,
        &no_columns,
        &no_columns,
        None,
    )
    .is_ok());
    assert!(validate_metadata(
        "list",
        &["id".to_owned(), "id".to_owned()],
        "id",
        20,
        100,
        &[],
        &no_defaults,
        &no_defaults,
        &no_defaults,
        &no_columns,
        &no_columns,
        None,
    )
    .is_err());
    assert!(validate_metadata(
        "list",
        &["id".to_owned()],
        "id",
        0,
        100,
        &[],
        &no_defaults,
        &no_defaults,
        &no_defaults,
        &no_columns,
        &no_columns,
        None,
    )
    .is_err());
}

#[test]
fn model_defaults_are_converted_to_rust_owned_values() {
    assert!(matches!(
        parse_model_default("uuid4", "").unwrap(),
        ModelDefaultValue::Uuid4
    ));
    assert!(matches!(
        parse_model_default("utc_now", "").unwrap(),
        ModelDefaultValue::UtcNow
    ));
    assert!(matches!(
        parse_model_default("str", "value").unwrap(),
        ModelDefaultValue::Str(value) if value == "value"
    ));
    assert!(parse_model_default("float", "NaN").is_err());
}
