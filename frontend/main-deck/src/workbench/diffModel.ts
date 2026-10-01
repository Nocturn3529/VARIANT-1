/** Unified patch parsing; all content remains text, never executable markup. */
export type DiffRow = {
  id: number; type: "meta" | "hunk" | "add" | "remove" | "context" | "notice" | "plain";
  text: string; oldLine?: number; newLine?: number; hunk?: number;
};
const MAX_CHARS = 256 * 1024;
const MAX_LINES = 6000;

export function parseDiff(value: string, fullContents = false) {
  const bounded = value.slice(0, MAX_CHARS);
  const lines = bounded.split(/\r?\n/);
  if (lines.at(-1) === "") lines.pop();
  const rows: DiffRow[] = [];
  let oldLine=0,newLine=0,oldRemaining=0,newRemaining=0,hunk: number | undefined;
  let added=0,removed=0;
  for (const [id,line] of lines.slice(0,MAX_LINES).entries()) {
    if (fullContents) {rows.push({id,type:"add",text:line,newLine:id+1});added++;continue;}
    const header = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line);
    if (header) {
      oldLine=Number(header[1]);oldRemaining=Number(header[2] ?? 1);
      newLine=Number(header[3]);newRemaining=Number(header[4] ?? 1);hunk=id;
      rows.push({id,type:"hunk",text:line,hunk});continue;
    }
    if (line.startsWith("diff --git ")) {hunk=undefined;oldRemaining=0;newRemaining=0;}
    const inHunk = hunk !== undefined && (oldRemaining>0 || newRemaining>0);
    if (inHunk && line.startsWith("+")) {
      rows.push({id,type:"add",text:line.slice(1),newLine:newLine++,hunk});newRemaining--;added++;
    } else if (inHunk && line.startsWith("-")) {
      rows.push({id,type:"remove",text:line.slice(1),oldLine:oldLine++,hunk});oldRemaining--;removed++;
    } else if (inHunk && line.startsWith(" ")) {
      rows.push({id,type:"context",text:line.slice(1),oldLine:oldLine++,newLine:newLine++,hunk});oldRemaining--;newRemaining--;
    } else if (line.startsWith("\\ No newline")) {
      rows.push({id,type:"notice",text:line,hunk});
    } else {
      rows.push({id,type:/^Binary files .+ differ$/.test(line)?"notice":"meta",text:line});
    }
  }
  return {rows,added,removed,truncated:value.length>MAX_CHARS || lines.length>MAX_LINES};
}

export const reviewPath=(root:string,file:string)=>`${root.replace(/[\\/]$/,"")}${root.includes("\\")?"\\":"/"}${file.replace(/[\\/]/g,root.includes("\\")?"\\":"/")}`;
export const reviewDeleted=(file:{status:string})=>file.status.trim()==="D" || file.status[1]==="D";
