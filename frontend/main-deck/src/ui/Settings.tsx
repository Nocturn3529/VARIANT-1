import type {ReactNode} from "react";
import {Switch} from "./Switch";

export function SettingsPage({children}: {children: ReactNode}) {
  return <div className="settings-page">{children}</div>;
}

export function SettingsSection({
  eyebrow,
  title,
  description,
  action,
  children,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return <section className="settings-section">
    <header className="settings-section__header">
      <div>
        {eyebrow ? <span>{eyebrow}</span> : null}
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
      {action ? <div className="settings-section__action">{action}</div> : null}
    </header>
    <div className="settings-section__rows">{children}</div>
  </section>;
}

export function SettingRow({
  title,
  description,
  control,
  children,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  control?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return <div className={["setting-row", className].filter(Boolean).join(" ")}>
    <div className="setting-row__copy">
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
      {children}
    </div>
    {control ? <div className="setting-row__control">{control}</div> : null}
  </div>;
}

export function SettingToggleRow({
  title,
  description,
  checked,
  disabled,
  onChange,
}: {
  title: string;
  description?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  onChange?: (next: boolean) => void;
}) {
  return <SettingRow
    title={title}
    description={description}
    control={<Switch
      checked={checked}
      disabled={disabled || !onChange}
      onChange={next => onChange?.(next)}
      aria-label={title}
    />}
  />;
}
