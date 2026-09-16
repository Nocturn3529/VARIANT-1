import type {ReactNode} from "react";

export type SwitchProps = {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** Bordered chip used by the composer Mutation control. */
  framed?: boolean;
  label?: ReactNode;
  caption?: ReactNode;
  decoration?: ReactNode;
  title?: string;
  id?: string;
  className?: string;
  "aria-label"?: string;
};

/** Shared on/off track. Live/Paused pills, tabs, and checkboxes stay local. */
export function Switch({
  checked,
  onChange,
  disabled,
  framed,
  label,
  caption,
  decoration,
  title,
  id,
  className,
  "aria-label": ariaLabel,
}: SwitchProps) {
  const named = typeof label === "string" ? label : undefined;
  return <button
    type="button"
    role="switch"
    id={id}
    className={["deck-switch", framed ? "deck-switch--framed" : "", checked ? "is-on" : "", className].filter(Boolean).join(" ")}
    aria-checked={checked}
    aria-label={ariaLabel || named}
    title={title}
    disabled={disabled}
    onClick={() => {
      if (!disabled) onChange(!checked);
    }}
  >
    {decoration}
    {label ? <span className="deck-switch__label">{label}</span> : null}
    <i className="deck-switch__track" aria-hidden="true"><b /></i>
    {caption ? <span className="deck-switch__caption">{caption}</span> : null}
  </button>;
}

export type SwitchRowProps = {
  title: string;
  detail: ReactNode;
  checked: boolean;
  disabled?: boolean;
  titleAttr?: string;
  onChange?: (next: boolean) => void;
  className?: string;
};

/** Settings row: copy on the left, switch on the right. */
export function SwitchRow({
  title,
  detail,
  checked,
  disabled,
  titleAttr,
  onChange,
  className,
}: SwitchRowProps) {
  return <div className={className} title={titleAttr}>
    <span>
      <strong>{title}</strong>
      {detail ? <small>{detail}</small> : null}
    </span>
    <Switch
      checked={checked}
      disabled={disabled || !onChange}
      onChange={next => onChange?.(next)}
      aria-label={title}
    />
  </div>;
}

export function createSwitchRow(className: string) {
  return function StyledSwitchRow(
    props: Omit<SwitchRowProps, "className">,
  ) {
    return <SwitchRow className={className} {...props} />;
  };
}
