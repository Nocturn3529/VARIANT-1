'use strict';
const {execFileSync}=require('node:child_process');
const path=require('node:path');
execFileSync(process.execPath,[path.join(__dirname,'test-frontend-maintainability.js'),'--live-swarm-repair'],{stdio:'inherit'});
