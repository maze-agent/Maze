from typing import Dict
import os
import threading
import os
import sys
import json
import joblib
import numpy as np
import pandas as pd
import threading
import xgboost as xgb
from datetime import datetime
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score, mean_squared_error
from sklearn.neural_network import MLPRegressor
from sklearn.linear_model import LinearRegression
from pathlib import Path


FEATURES:Dict = {
    "llm_process": [
        "text_length",
        "token_count",
        "batch_size",
        "reason",
    ],
    "vlm_process": [
        "image_height", "image_width", "image_area", "image_aspect_ratio", "batch_size",
        "image_entropy", "edge_density", "text_area_ratio", "text_block_count",
        "avg_brightness", "brightness_variance", "prompt_length", "prompt_token_count"
    ],
    "llm_fuse": [
        "prompt_length", "prompt_token_count", "text1_length", "text2_length",
        "text1_token_count", "text2_token_count", "reason"
    ],
    "speech_process":[
        "duration",
        "audio_array_len",
        "audio_entropy",
        "audio_energy"
    ],
}

class ExecTimePredictor:
    def __init__(self,task_name,model_dir,min_sample4train,min_sample4incremental):
        self.task_name = task_name
        self.model_dir= os.path.join(model_dir, "model")
        self.data_dir= os.path.join(model_dir, "data")
        self.model_path = os.path.join(self.model_dir, "model.pkl")
        self.data_path = os.path.join(self.data_dir, "data.csv")
        self.min_sample4train = min_sample4train
        self.min_sample4incremental = min_sample4incremental
        self.last_training_size= 0
        self.features= FEATURES[task_name]

        os.makedirs(self.model_dir, exist_ok= True)
        os.makedirs(self.data_dir, exist_ok= True)
         
        # Model-related attributes
        self.model= None
        self.scaler= None
        self.best_mae= float('inf')

        self._load_model()
        if os.path.exists(self.data_path):
            self.last_training_size= len(pd.read_csv(self.data_path))

    def _load_model(self):
        """Loads the model and scaler from disk if they exist."""
        if os.path.exists(self.model_path):
            try:
                model_data= joblib.load(self.model_path)
                self.model= model_data['model']
                self.scaler= model_data['scaler']
                self.best_mae= model_data.get('mae', float('inf')) # Load MAE if available
            except Exception as e:
                print(f"Error loading model for '{self.task_name}': {e}")
                self.model, self.scaler= None, None

    def collect_data(self, features, execution_time):
        # 1. Get features, Filter to keep only relevant features
        current_features = {}
        for feature in self.features:
            if features and feature in features:
                current_features[feature] = features[feature]
            else:
                current_features[feature] = 0
                print(f"Warning: Missing feature '{feature}', using default value: {current_features[feature]}")
                
        # 2. Prepare and save data
        data_point= {
            "task_name": self.task_name,
            "timestamp": datetime.now().isoformat(),
            **current_features,
            "execution_time": execution_time
        }
        new_data_df= pd.DataFrame([data_point])
        try:
            if os.path.exists(self.data_path):
                existing_data_df= pd.read_csv(self.data_path)
                combined_df= pd.concat([existing_data_df, new_data_df], ignore_index= True)
            else:
                combined_df= new_data_df
            combined_df.to_csv(self.data_path, index= False)
            print(f"Data saved. Total samples for '{self.task_name}': {len(combined_df)}")
            # 3. Check for training triggers
            current_size= len(combined_df)
            new_sample_size= current_size- self.last_training_size
            should_train = (self.model is None and current_size >= self.min_sample4train) or \
                           (self.model is not None and new_sample_size >= self.min_sample4incremental)
            if should_train:
                print(f"Training model for '{self.task_name}'...")
                df_for_training = combined_df.copy()
                is_incremental = self.model is not None
                
                self.train_model(df_for_training, is_incremental)
                self.last_training_size = current_size

        except Exception as e:
            print(f"Error during data collection or training trigger: {e}")

    def _clean_data(self, y):
        """
        Removes outliers from the target variable using the IQR method.

        Args:
            y (pd.Series or np.array): The target variable (execution times).

        Returns:
            pd.Index: The indices of the valid (non-outlier) data.
        """
        q1= y.quantile(0.25)
        q3= y.quantile(0.75)
        iqr= q3- q1
        lower_bound= q1- 1.5* iqr
        upper_bound= q3+ 1.5* iqr
        valid_indices= y[(y>= lower_bound)& (y<= upper_bound)].index
        print(f"Data cleaning: Kept {len(valid_indices)} out of {len(y)} samples after outlier removal.")
        return valid_indices
    
    def _print_evaluation_metrics(self, y_true, y_pred, stage_name):
        """Calculates and prints key regression metrics."""
        mae= mean_absolute_error(y_true, y_pred)
        mse= mean_squared_error(y_true, y_pred)
        r2= r2_score(y_true, y_pred)
        print(f"\n--- {stage_name} Evaluation ---")
        print(f"  MAE: {mae:.4f} (Mean Absolute Error)")
        print(f" RMSE: {np.sqrt(mse):.4f} (Root Mean Squared Error)")
        print(f"   R²: {r2:.4f} (Coefficient of Determination)")
        return mae, r2
    
    def train_model(self, data_df, incremental= False):
        # Prepare data, removing outliers
        clean_indices= self._clean_data(data_df["execution_time"])
        clean_df= data_df.loc[clean_indices]
        X= clean_df[self.features]
        y= clean_df["execution_time"]
        X_train, X_val, y_train, y_val= train_test_split(X, y, test_size= 0.2, random_state= 42)

        # Handle scaling
        if not incremental or self.scaler is None:
            self.scaler= StandardScaler()
            X_train_scaled= self.scaler.fit_transform(X_train)
        else:
            X_train_scaled= self.scaler.transform(X_train)
        X_val_scaled= self.scaler.transform(X_val)

        dtrain = xgb.DMatrix(X_train_scaled, label=y_train, feature_names=self.features)
        dval = xgb.DMatrix(X_val_scaled, label=y_val, feature_names=self.features)
        eval_list = [(dtrain, "train"), (dval, "eval")]
        params = {
            "objective": "reg:squarederror", "learning_rate": 0.05,
            "max_depth": 4, "min_child_weight": 3, "subsample": 0.8,
            "colsample_bytree": 0.8, "random_state": 42, "tree_method": "hist"
        }
        print("Starting XGBoost training...")
        self.model = xgb.train(
            params,
            dtrain,
            num_boost_round=50 if incremental and self.scaler is not None else 100,
            evals=eval_list,
            early_stopping_rounds=10 if incremental and self.scaler is not None else 20,
            xgb_model=self.model if incremental else None
        )
        y_pred_val = self.model.predict(dval)
        

        # Evaluate and save
        y_pred_val= self.model.predict(dval)
        current_mae, _= self._print_evaluation_metrics(y_val, y_pred_val, "Validation Set")
        if current_mae < self.best_mae: 
            self.best_mae= current_mae
            self._save_model()
            self._load_model()

    def _save_model(self):
        if self.model and self.scaler:
            model_data= {
                'model': self.model,
                'scaler': self.scaler,
                'mae': self.best_mae,
            }
            joblib.dump(model_data, self.model_path)
            print(f"Model for '{self.task_name}' saved to {self.model_path}.")

    def predict(self, features):
        if self.model is None:
            print("Warning: Model is not trained. Returning default execution time.")
            return 1.0

        current_features= {}
        for feature in self.features:
            if features and feature in features:
                current_features[feature] = features[feature]
            else:
                current_features[feature] = 0  # default value
                print(f"Warning: Missing feature '{feature}', using default value: {current_features[feature]}")

        X_df= pd.DataFrame([current_features])
        X_scaled= self.scaler.transform(X_df)

        dtest = xgb.DMatrix(X_scaled, feature_names=self.features)
        prediction = self.model.predict(dtest)[0]
        pred_time = max(0.1, float(prediction))

        return pred_time

class Predictor:
    def __init__(self,model_dir=os.path.expanduser("~")+"/.cache/maze",min_sample4train=10,min_sample4incremental=10):
        if not Path(model_dir).exists():
            Path(model_dir).mkdir(parents=True)
        self.predictors = {}
        for task_name in FEATURES:
            self.predictors[task_name]= ExecTimePredictor(
                task_name = task_name,
                model_dir = os.path.join(model_dir, task_name),
                min_sample4train= min_sample4train,
                min_sample4incremental= min_sample4incremental
            )
    
    def predict(self,task_name,features):
        return self.predictors[task_name].predict(features)

    def collect_data(self,task_name,features,execution_time):
        self.predictors[task_name].collect_data(features,execution_time)
