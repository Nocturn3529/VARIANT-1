(() => {
  "use strict";
  if (new URLSearchParams(window.location.search).get("fixture") !== "1") {
    return;
  }
  const script = document.createElement("script");
  script.type = "module";
  script.src = "./dist/fixture.js";
  document.head.appendChild(script);
})();
