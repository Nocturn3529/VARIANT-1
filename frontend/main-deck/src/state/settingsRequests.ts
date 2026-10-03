import {createRequestIdFactory} from "./storePrimitives";
type Entry={id:string;operation:string;key:string;timer:ReturnType<typeof setTimeout>};

/** No retries of writes: correlated replies settle only the current request. */
export function settingsRequests(prefix:string,send:(command:Record<string,unknown>&{type:string})=>boolean,onPending:(key:string,pending:boolean)=>void,onError:(key:string,message:string)=>void){
  const entries=new Map<string,Entry>(),nextId=createRequestIdFactory(prefix);
  function finish(key:string){const entry=entries.get(key);if(!entry)return;clearTimeout(entry.timer);entries.delete(key);onPending(key,false);}
  return {
    request(key:string,operation:string,command:Record<string,unknown>&{type:string},timeout=20000){
      if(entries.has(key))return false;const id=nextId(operation);
      const timer=setTimeout(()=>{finish(key);onError(key,"No response received. Refresh to check the result before retrying.");},timeout);
      entries.set(key,{id,operation,key,timer});onPending(key,true);
      if(!send({...command,request_id:id})){finish(key);onError(key,"Request could not be sent.");return false;}return id;
    },
    take(message:Record<string,unknown>){const entry=[...entries.values()].find(row=>row.id===message.request_id && row.operation===message.operation);if(!entry)return;finish(entry.key);return entry;},
    cancelKey(key:string){finish(key);},
    cancel(message=""){for(const key of [...entries.keys()]){finish(key);if(message)onError(key,message);}},
  };
}
