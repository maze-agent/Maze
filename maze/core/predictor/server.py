import signal
import os

from fastapi import FastAPI, Request, HTTPException

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
        features = data.get("features") or {}
        if not isinstance(features, dict):
            raise HTTPException(status_code=400, detail="features must be a JSON object")

        predict_time = predictor.predict(task_name, features)
        return {
            "status": "success",
            "predict_time": float(predict_time),
            "prediction_source": "malearn",
        }
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"unknown or missing field: {e}") from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/collect_data")
async def collect_data(req:Request):
    try:
        data = await req.json()
        task_name = data["task_name"]
        features = data.get("features") or {}
        if not isinstance(features, dict):
            raise HTTPException(status_code=400, detail="features must be a JSON object")
        execution_time = data["execution_time"]
        predictor.collect_data(task_name, features, execution_time)
        return {"status":"success"}
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"unknown or missing field: {e}") from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


