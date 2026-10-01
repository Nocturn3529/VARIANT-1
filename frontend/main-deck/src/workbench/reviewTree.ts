import type {WorkbenchGitFile} from "../types";
export type ReviewTreeRow={kind:"folder";path:string;name:string;depth:number;count:number}|{kind:"file";file:WorkbenchGitFile;name:string;depth:number};
type Folder={path:string;name:string;folders:Map<string,Folder>;files:WorkbenchGitFile[];count:number};

/** Repository-relative paths are grouped without losing their identity to basenames. */
export function reviewTree(files:readonly WorkbenchGitFile[],closed:ReadonlySet<string>,query=""):ReviewTreeRow[]{
  const root:Folder={path:"",name:"",folders:new Map(),files:[],count:0};
  for(const file of files){
    if(!file.path.toLowerCase().includes(query.toLowerCase()))continue;
    const parts=file.path.split("/");let folder=root;folder.count++;
    for(const name of parts.slice(0,-1)){
      const path=folder.path?`${folder.path}/${name}`:name;
      if(!folder.folders.has(name))folder.folders.set(name,{path,name,folders:new Map(),files:[],count:0});
      folder=folder.folders.get(name)!;folder.count++;
    }
    folder.files.push(file);
  }
  const rows:ReviewTreeRow[]=[];
  function visit(folder:Folder,depth:number){
    for(const child of [...folder.folders.values()].sort((a,b)=>a.name.localeCompare(b.name))){
      rows.push({kind:"folder",path:child.path,name:child.name,depth,count:child.count});
      if(query || !closed.has(child.path))visit(child,depth+1);
    }
    for(const file of folder.files.sort((a,b)=>a.path.localeCompare(b.path)))rows.push({kind:"file",file,name:file.path.split("/").at(-1) || file.path,depth});
  }
  visit(root,0);return rows;
}
