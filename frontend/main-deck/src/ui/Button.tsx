import type {ButtonHTMLAttributes} from "react";

export type ButtonTone = "ghost" | "primary" | "icon" | "quiet";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: ButtonTone;
};

/** Shared control. Composer, window chrome, tabs, and list rows stay local. */
export function Button({
  tone = "ghost",
  type = "button",
  className,
  ...props
}: ButtonProps) {
  return <button
    type={type}
    className={[
      "deck-button",
      tone === "ghost" ? "" : `deck-button--${tone}`,
      className,
    ].filter(Boolean).join(" ")}
    {...props}
  />;
}
