"use strict";
require("node:child_process").execFileSync(process.execPath,[require("node:path").join(__dirname,"test-frontend-maintainability.js"),"--peers"],{stdio:"inherit"});
