'use strict';
const path=require('node:path'),Module=require('node:module');
const {buildSync}=require('esbuild');
const file=path.join(__dirname,'.generated-peer-transport.cjs');
const result=buildSync({entryPoints:[path.join(__dirname,'test-peer-transport-entry.ts')],bundle:true,platform:'node',format:'cjs',write:false,logLevel:'silent'});
const compiled=new Module(file,module);compiled.filename=file;compiled.paths=Module._nodeModulePaths(__dirname);
compiled._compile(result.outputFiles[0].text,file);compiled.exports.run();
