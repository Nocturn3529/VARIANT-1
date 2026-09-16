"use strict";
const {execFileSync}=require("node:child_process"),path=require("node:path");
execFileSync(process.execPath,[path.join(__dirname,"test-frontend-maintainability.js"),"--mic-reconnect"],{stdio:"inherit"});
