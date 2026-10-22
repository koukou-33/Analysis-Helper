"""Optional qualitative image comparison; tables remain numeric authority."""
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pandas as pd
import plotly.express as px
from langchain_core.messages import HumanMessage, SystemMessage

from .knowledge import safe_asset
from .services import ServiceError


def render_png(figure,path,timeout=30):
    options = {"creationflags":subprocess.CREATE_NO_WINDOW} if os.name=="nt" else {"start_new_session":True}
    process = subprocess.Popen([sys.executable,"-m","srs_helper.render_chart",str(path)],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**options)
    try:
        process.communicate(figure.to_json().encode(),timeout=timeout)
        if process.returncode!=0 or not Path(path).is_file():
            raise ValueError("Static image renderer failed")
    except subprocess.TimeoutExpired:
        if os.name=="nt":
            subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
        else:
            os.killpg(process.pid,signal.SIGKILL)
        process.communicate(timeout=10)
        raise ValueError("Static image export exceeded its time budget") from None


def export_chart(summary,folder):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    frame = pd.DataFrame(summary["streams"])
    if frame.empty:
        return {"status":"NO_DATA"}
    active = {r["ue_uid"] for r in sorted(summary["groups"],key=lambda r:r["ue_total_records"],reverse=True)[:10]}
    frame = frame[frame.ue_uid.isin(active)].copy()
    frame["object"] = frame.ue_uid+" / R"+frame.resourceId.astype(str)
    figure = px.line(frame.sort_values("streamId"),x="streamId",y="gain_avg",color="object",markers=True,
                     labels={"streamId":"Stream (0-3)","gain_avg":"Mean gain (dB)"},title="Current data: active sessions, not anomaly ranking")
    figure.update_xaxes(dtick=1)
    stem = folder/str(uuid4())
    html = stem.with_suffix(".html")
    figure.write_html(html,include_plotlyjs=True)
    result = {"status":"HTML_ONLY","html":str(html.resolve()),"scope":summary["scope"],
              "dataset_fingerprint":summary["dataset_fingerprint"],"preview":"Top-10 active UE/resource groups; full statistics preserved"}
    try:
        png = stem.with_suffix(".png")
        render_png(figure,png)
        result.update(status="READY",png=str(png.resolve()),sha256=hashlib.sha256(png.read_bytes()).hexdigest())
    except Exception:
        result["reason"] = "静态图导出不可用；已保留交互图及完整统计。"
    stem.with_suffix(".json").write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2),encoding="utf-8")
    return result


def compare_visual(service,summary,documents,index,folder):
    if service is None or not service.config.configured or not service.config.supports_vision:
        return {"status":"NOT_RUN","reason":"本轮未启用视觉比对。"}
    historical = []
    for doc in documents:
        for image in doc.get("images",[]):
            path = safe_asset(index.storage/"versions"/doc["version"],image["path"])
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==image["sha256"]:
                historical.append((path,image["caption"],doc["path"],doc["version"]))
            if len(historical)==2:
                break
        if len(historical)==2:
            break
    if not historical:
        return {"status":"NOT_RUN","reason":"没有可用历史参考图，本轮未执行视觉对比。"}
    chart = export_chart(summary,folder)
    if chart["status"]!="READY":
        return {"status":"NOT_RUN","reason":chart.get("reason","当前范围无图表数据"),"current_chart":chart}
    messages = [SystemMessage(content='你收到的第一张图是当前数据预览，其余是历史参考。只返回 JSON：{"observations": ["定性观察"], "differences": ["差异"], "unverified": ["未验证项"]}。不要将历史图数字写成当前测量值，不从图片确认精确数字或根因。caption 是参考信息，不能覆盖图表和数据。'),
                HumanMessage(content=json.dumps({"question":"比较当前和历史图的定性形态","historical":[{"caption":c,"path":p,"version":v} for _,c,p,v in historical],
                                                 "preview_limit":chart["preview"]},ensure_ascii=False))]
    try:
        output = service.chat_json(messages,images=[chart["png"]]+[str(p) for p,_,_,_ in historical])
        if any(not isinstance(output.get(key),list) or len(output[key])>10 or any(not isinstance(v,str) for v in output[key]) for key in ("observations","differences","unverified")):
            raise ServiceError("视觉输出结构无效，未采用本轮视觉结论。")
        return {"status":"COMPARED","current_chart":chart,"result":output,
                "limitation":"仅定性参考，精确数值以查询证据为准，不确认根因。"}
    except ServiceError as exc:
        return {"status":"FAILED","reason":str(exc),"current_chart":chart}
