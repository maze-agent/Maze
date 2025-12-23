from pyexpat import features
from typing import Dict
from fastapi import FastAPI
import signal
import os
from fastapi import FastAPI, WebSocket, Request, HTTPException
from maze.core.predictor.predictor import Predictor

app = FastAPI()
predictor = Predictor()

def signal_handler(signum, frame):
   os._exit(1)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

@app.post("/predict")
async def predict(req:Request):
    try:
        data = await req.json()
        task_name = data["task_name"]
        features = data["features"]
        
        predictor.predict(task_name, features)
        return {"status":"success","predict_time": 1}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/collect_data")
async def collect_data(req:Request):
    try:
        data = await req.json()
        task_name = data["task_name"]
        features = data["features"]
        execution_time = data["execution_time"]
        predictor.collect_data(task_name, features, execution_time)
        return {"status":"success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



