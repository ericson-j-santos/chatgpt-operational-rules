from __future__ import annotations

import json

import win32com.client  # type: ignore


def trigger_type_name(value:int)->str:
    return {0:"Event",1:"Time",2:"Daily",3:"Weekly",4:"Monthly",5:"MonthlyDOW",6:"Idle",7:"Registration",8:"Boot",9:"Logon",11:"SessionStateChange"}.get(int(value),str(value))


def collect(folder,rows:list[dict[str,object]])->None:
    path=str(folder.Path or "")
    is_microsoft=path.casefold().startswith(r"\microsoft")
    for task in folder.GetTasks(1):
        d=task.Definition
        p=d.Principal
        run_level=int(getattr(p,"RunLevel",0) or 0)
        logon_type=int(getattr(p,"LogonType",0) or 0)
        user=str(getattr(p,"UserId","") or "")
        actions=[]
        for a in d.Actions:
            actions.append({
                "type":int(a.Type),
                "path":str(getattr(a,"Path","") or ""),
                "arguments":str(getattr(a,"Arguments","") or ""),
                "working_directory":str(getattr(a,"WorkingDirectory","") or ""),
            })
        elevated=run_level==1 or user.casefold() in {"system","nt authority\\system","localsystem"}
        reqsys_related=any(
            any(k in (str(x.get("path",""))+" "+str(x.get("arguments",""))).casefold() for k in ("reqsys","chatgpt","pc24x7","gateway","automation","remote"))
            for x in actions
        )
        if (not is_microsoft) and (elevated or reqsys_related):
            rows.append({
                "path":str(task.Path),
                "enabled":bool(task.Enabled),
                "state":int(task.State),
                "last_result":int(task.LastTaskResult),
                "principal_user":user,
                "principal_logon_type":logon_type,
                "principal_run_level":run_level,
                "triggers":[trigger_type_name(t.Type) for t in d.Triggers],
                "actions":actions,
                "elevated_candidate":elevated,
                "reqsys_related":reqsys_related,
            })
    for sub in folder.GetFolders(0):
        collect(sub,rows)


def main()->int:
    svc=win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    rows:list[dict[str,object]]=[]
    collect(svc.GetFolder("\\"),rows)
    print(json.dumps({"count":len(rows),"tasks":rows,"secret_value_exposed":False},ensure_ascii=True,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
