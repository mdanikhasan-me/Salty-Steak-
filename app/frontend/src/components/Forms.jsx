export function Field({ label, hint, error, children, className = "" }) {
  return (
    <label className={`field ${className}`}>
      <span className="field__label">{label}</span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
      {error ? <span className="field__error">{error}</span> : null}
    </label>
  );
}

export function Checkbox({ label, description, ...props }) {
  return (
    <label className="check-field">
      <input type="checkbox" {...props} />
      <span>
        <strong>{label}</strong>
        {description ? <small>{description}</small> : null}
      </span>
    </label>
  );
}

export function FormActions({ children }) {
  return <div className="form-actions">{children}</div>;
}
