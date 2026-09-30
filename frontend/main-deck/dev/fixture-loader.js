(() => {
  "use strict";
  if (new URLSearchParams(window.location.search).get("fixture") !== "1") {
    return;
  }
  const script = document.createElement("script");
  script.type = "module";
  script.src = new URL("../dist/fixture.js", document.currentScript.src).href;
  document.head.appendChild(script);
})();
