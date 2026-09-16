import {useEffect, useState} from "react";

/** Blob-backed frames avoid Chromium's large data-URL PDF viewer limit. */
export function PdfPreview({source, label}: {source: string; label: string}) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    setUrl(""); setError("");
    if (!source) return;
    if (!source.startsWith("data:")) { setUrl(source); return; }
    let objectUrl = "";
    try {
      const match = /^data:application\/pdf(?:;[^,]*)?;base64,(.*)$/s.exec(source);
      if (!match) throw new Error("The PDF data could not be decoded.");
      const bytes = Uint8Array.from(atob(match[1]), char => char.charCodeAt(0));
      objectUrl = URL.createObjectURL(new Blob([bytes], {type: "application/pdf"}));
      setUrl(objectUrl);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    return () => { if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [source]);
  if (error) return <div className="workbench-preview__state" role="alert"><strong>PDF preview unavailable</strong><span>{error}</span></div>;
  if (!url) return <div className="workbench-preview__state" role="status">Loading PDF…</div>;
  return <iframe className="workbench-file-preview__media" src={url} title={label} aria-label={`PDF preview: ${label}`}/>;
}
