import os
import sys
import json
import redis
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

class ExecTimePredictor:
    """
    A unified predictor for estimating the execution time of various tools,
    including a specialized mode for text inference. It supports incremental
    learning as more data is collected.
    """
    DEFAULT_MODEL_TYPE = 'xgb'  #  'mlp'  'linear'

    def __init__(self, tool_type, pred_model_dir, min_sample4train= 50, min_sample4incremental= 20):
        """
        Initializes the Execution Time Predictor.

        Args:
            tool_type (str): The type of tool (e.g., 'inference', 'ocr').
            model_dir (str): Directory to store models and data.
            min_sample4train (int): Minimum samples required for initial training.
            min_sample4incremental (int): Minimum new samples to trigger incremental training.
        """        
        # Define features for different tool types, including the special 'inference' type.

        self.tool_types= {
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
            "llm_fuse2In1": [
                "prompt_length", "prompt_token_count", "text1_length", "text2_length",
                "text1_token_count", "text2_token_count", "reason"
            ],
            "speech_process":[
                "duration",
                "audio_entropy",
                "audio_energy"
            ],
        }
        if tool_type not in self.tool_types:
            raise ValueError(f"Invalid tool_type: {tool_type}. Must be one of {list(self.tool_types.keys())}")
        self.default_values= {
            "reason": 0,
            "batch_size": 1
        }
        self.tool_type= tool_type
        self.model_dir= pred_model_dir
        self.data_dir= os.path.join(pred_model_dir, "data")
        self.data_path= os.path.join(self.data_dir, f"{tool_type}_data.csv")
        self.model_path= os.path.join(self.model_dir, f"{tool_type}_model.pkl")

        self.min_sample4train= min_sample4train # Minimum number of samples required for initial training
        self.min_sample4incremental= min_sample4incremental # Minimum number of samples required for incremental training
        self.last_training_size= 0 # Amount of data during last training

        os.makedirs(self.model_dir, exist_ok= True)
        os.makedirs(self.data_dir, exist_ok= True)

        self.features= self.tool_types[tool_type]

        # Model-related attributes
        self.model= None
        self.scaler= None
        self.best_mae= float('inf')
        self.model_type = self.DEFAULT_MODEL_TYPE  #

        
        self._model_lock = threading.Lock()

        # Load existing model and data state
        self._load_model()
        if os.path.exists(self.data_path):
            self.last_training_size= len(pd.read_csv(self.data_path))

    def _load_model(self):
        """Loads the model and scaler from disk if they exist."""
        with self._model_lock:
            if os.path.exists(self.model_path):
                try:
                    model_data= joblib.load(self.model_path)
                    self.model= model_data['model']
                    self.scaler= model_data['scaler']
                    self.best_mae= model_data.get('mae', float('inf')) # Load MAE if available
                    self.model_type = model_data.get('model_type', self.model_type)  #

                    print(f"Model for '{self.tool_type}' loaded successfully from {self.model_path}")
                except Exception as e:
                    print(f"Error loading model for '{self.tool_type}': {e}")
                    self.model, self.scaler= None, None

    def _save_model(self):
        """Saves the current model and scaler to disk."""
        if self.model and self.scaler:
            with self._model_lock:
                model_data= {
                    'model': self.model,
                    'scaler': self.scaler,
                    'mae': self.best_mae,
                    'model_type': self.model_type  #

                }
                joblib.dump(model_data, self.model_path)
                print(f"Model for '{self.tool_type}' saved to {self.model_path}.")
    
    def collect_data(self, actual_time, features= None, task_id= "N/A"):
        """
        Collects and saves new execution data, and triggers training if conditions are met.
        Args:
            actual_time (float): The actual execution time in seconds.
            text (str, optional): The input text, required if tool_type is 'inference'.
            features (dict, optional): A dictionary of features for other tool types.
            task_id (str, optional): An identifier for the task.
        """
        print(f"\nCollecting data for '{self.tool_type}' task '{task_id}' (Time: {actual_time:.2f}s)...")
        # 1. Get features, Filter to keep only relevant features
        current_features = {}
        for feature in self.features:
            if features and feature in features:
                current_features[feature] = features[feature]
            else:
                current_features[feature] = self.default_values.get(feature)  # 0
                print(f": task'{task_id}''{feature}'use: {current_features[feature]}")
        # 2. Prepare and save data
        data_point= {
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
            **current_features,
            "execution_time": actual_time
        }
        new_data_df= pd.DataFrame([data_point])
        try:
            if os.path.exists(self.data_path):
                existing_data_df= pd.read_csv(self.data_path)
                combined_df= pd.concat([existing_data_df, new_data_df], ignore_index= True)
            else:
                combined_df= new_data_df
            combined_df.to_csv(self.data_path, index= False)
            print(f"Data saved. Total samples for '{self.tool_type}': {len(combined_df)}")
            # 3. Check for training triggers
            current_size= len(combined_df)
            new_sample_size= current_size- self.last_training_size
            should_train = (self.model is None and current_size >= self.min_sample4train) or\
                           (self.model is not None and new_sample_size >= self.min_sample4incremental)
            if should_train:
                print(f"...")
                #

                df_for_training = combined_df.copy()
                is_incremental = self.model is not None
                
                #

                training_thread = threading.Thread(
                    target=self.train_model,
                    args=(df_for_training, is_incremental)
                )
                training_thread.daemon = True  #

                training_thread.start()
                
                self.last_training_size = current_size #


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

    def train_model(self, data_df, incremental= False):
        """
        Trains or updates the model based on model_type.
        Args:
            data_df (pd.DataFrame): The training data.
            incremental (bool): Flag for incremental training.
        """
        # Ensure there is data to train on
        if data_df.shape[0]< 5:
            print("Not enough data to train. Need at least 5 samples.")
            return

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

        if self.model_type == 'xgb':
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
        elif self.model_type == 'mlp':
            print("Starting MLPRegressor training...")
            self.model = MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=300, random_state=42)
            self.model.fit(X_train_scaled, y_train)
            y_pred_val = self.model.predict(X_val_scaled)
        elif self.model_type == 'linear':
            print("Starting LinearRegression training...")
            self.model = LinearRegression()
            self.model.fit(X_train_scaled, y_train)
            y_pred_val = self.model.predict(X_val_scaled)
        else:
            print(f"Unknown model_type: {self.model_type}")
            return

        # Evaluate and save
        y_pred_val= self.model.predict(dval)
        current_mae, _= self._print_evaluation_metrics(y_val, y_pred_val, "Validation Set")
        if current_mae< self.best_mae:
            print(f"New best model found! MAE improved from {self.best_mae:.4f} to {current_mae:.4f}.")
            self.best_mae= current_mae
            self._save_model()
        else:
            print(f"Training did not improve MAE. Keeping previous model (Best MAE: {self.best_mae:.4f}).")
            # Reload the best model to discard the one just trained
            self._load_model()
            
    def predict(self, features= None, task_id= None):
        """
        Predicts execution time for a given input.

        Args:
            text (str, optional): The input text, for 'inference' type.
            features (dict, optional): Feature dictionary for other tool types.

        Returns:
            float: The predicted execution time in seconds, or None if prediction fails.
        """
        with self._model_lock:
            if self.model is None or self.scaler is None:
                print("Model not available. Please collect data and train first.")
                return None

            # 1. Get features
            current_features= {}
            for feature in self.features:
                if features and feature in features:
                    current_features[feature] = features[feature]
                else:
                    current_features[feature] = self.default_values.get(feature)  # fallback to 0 if no default
                    print(f"Warning: Missing feature '{feature}', using default value: {current_features[feature]}")

            # 2. Prepare for prediction
            X_df= pd.DataFrame([current_features])
            X_scaled= self.scaler.transform(X_df)

            if self.model_type == 'xgb':
                dtest = xgb.DMatrix(X_scaled, feature_names=self.features)
                prediction = self.model.predict(dtest)[0]
            else:
                prediction = self.model.predict(X_scaled)[0]
            pred_time = max(0.1, float(prediction))

            #

            if task_id is not None:
                pred_record = {
                    "task_id": task_id,
                    "timestamp": datetime.now().isoformat(),
                    **current_features,
                    "predicted_time": pred_time
                }
                pred_log_path = os.path.join(self.data_dir, f"{self.tool_type}_pred_log.csv")
                pred_df = pd.DataFrame([pred_record])
                if os.path.exists(pred_log_path):
                    old_df = pd.read_csv(pred_log_path)
                    new_df = pd.concat([old_df, pred_df], ignore_index=True)
                else:
                    new_df = pred_df
                new_df.to_csv(pred_log_path, index=False)

            return pred_time

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

    def get_feature_importance(self):
        """Returns the feature importance scores from the trained model."""
        if self.model_type == 'xgb' and self.model:
            return self.model.get_score(importance_type='gain')
        elif self.model_type == 'linear' and self.model:
            return dict(zip(self.features, self.model.coef_))
        elif self.model_type == 'mlp' and self.model:
            return None  # MLPRegressor
        return None

class DAGTaskPredictor:
    """
    Manages multiple ExecTimePredictor instances and serves as the main interface
    for a DAG scheduler. It fetches features for a task from Redis based on its
    parent's task ID before dispatching to the correct predictor engine.
    """
    DEFAULT_MODEL_TYPE = ExecTimePredictor.DEFAULT_MODEL_TYPE

    def __init__(self, redis_ip: str, redis_port: int, model_dir: str, min_sample4train: int, min_sample4incremental: int):
        """
        Initializes the dispatcher.
        Args:
            redis_ip (str): The IP address of the Redis server.
            redis_port (int): The port of the Redis server.
            model_dir (str): The base directory where ML models for each tool type are stored.
        """
        self.redis_ip= redis_ip
        self.redis_port= redis_port
        self.redis_client= redis.Redis(host= self.redis_ip, port= self.redis_port)
        # A mapping from the function name in your DAGs to a standardized tool_type
        # that ExecTimePredictor understands.
        self.func_name_to_tool_type = {
            # File processing workflow
            "llm_process": "llm_process",
            "llm_fuse": "llm_fuse2In1",
            # Vision workflow
            "vlm_process": "vlm_process",
            # Speech workflow
            "speech_process": "speech_process"
        }
        # This dictionary will hold the specialized predictor instances (the "engines")
        self.predictors= {}
        unique_tool_types= set(self.func_name_to_tool_type.values())

        print("Initializing predictor engines...")
        for tool_type in unique_tool_types:
            print(f"  -> Creating predictor for tool_type: '{tool_type}'")
            # Each tool_type gets its own dedicated ExecTimePredictor instance
            self.predictors[tool_type]= ExecTimePredictor(
                tool_type= tool_type,
                pred_model_dir= os.path.join(model_dir, tool_type), # Give each model its own sub-folder
                min_sample4train= min_sample4train,
                min_sample4incremental= min_sample4incremental
                #  model_type
            )

    def get_mapped_tool_type(self, curr_func_name):
        """
        If curr_func_name contains a key in func_name_to_tool_type,
        Then the tool type corresponding to a is returned
        """
        for func_name in self.func_name_to_tool_type:
            if func_name in curr_func_name:
                return self.func_name_to_tool_type[func_name]
        return None

    def collect_data_for_task(self, task_id: str, func_name: str, record_json: dict):
        """
        task
        """
        if not record_json:
            print(f"Error: Missing record for task '{task_id}' during data collection.")
            return

        #

        task_features = record_json.get("curr_task_feat")
        execution_time = record_json.get("end_time", 0) - record_json.get("start_time", 0)
        tool_type = self.get_mapped_tool_type(func_name)

        if task_features and tool_type and tool_type in self.predictors:
            print(f"-> Calling data collection for '{func_name}' (type: {tool_type})...")
            self.predictors[tool_type].collect_data(
                actual_time=execution_time,
                features=task_features,
                task_id=task_id
            )
        else:
            print(f"Warning: No features or predictor found for '{func_name}'. Data not collected.")


    def predict(self, succ_func_name: str, succ_task_type: str, pred_task_ids: list) -> float:
        """
        
        """
        # 1. 
        HEURISTIC_TIME = {
            'gpu': 15.0,
            'cpu': 3.0,
            'io': 1.0,
        }
        #

        DEFAULT_HEURISTIC = HEURISTIC_TIME['io']         
        # --- MODIFICATION START ---
        # 2. (succ_func_name)
        
        # 2a. 
        succ_tool_type = self.get_mapped_tool_type(succ_func_name)
        predictor = self.predictors.get(succ_tool_type)

        if not predictor:
            print(f"Warning: No predictor for successor '{succ_func_name}'. Skipping prediction.")
            return HEURISTIC_TIME.get(succ_task_type, DEFAULT_HEURISTIC)

        # 2b. (pred_task_ids)
        aggregated_features = {}

        print(f"Aggregating features for '{succ_func_name}' of type {succ_task_type} from {len(pred_task_ids)} predecessor(s)...")
        for pred_id in pred_task_ids:
            try:
                pred_record_raw = self.redis_client.get(f"result:{pred_id}")
                if not pred_record_raw:
                    print(f"Warning: Could not find record for predecessor task '{pred_id}' in Redis.")
                    continue
                pred_record = json.loads(pred_record_raw)
                all_succ_features_map = pred_record.get("succ_task_feat", {})
                #

                features_for_this_succ = all_succ_features_map.get(succ_func_name)
                if features_for_this_succ:
                    #

                    for key, value in features_for_this_succ.items():
                        aggregated_features[key] = value
            except Exception as e:
                print(f"Error processing predecessor '{pred_id}' features: {e}")
        if not aggregated_features:
            print(f"Warning: No valid features aggregated for '{succ_func_name}'. Cannot predict.")
            return HEURISTIC_TIME.get(succ_task_type, DEFAULT_HEURISTIC)
            
        # 2c. 
        print(f"Predicting for '{succ_func_name}' with aggregated features...")
        est_time = predictor.predict(features= aggregated_features)
        if est_time is None or est_time < 0:
            print(f"Predictor for '{succ_func_name}' returned None. Using default priority: {HEURISTIC_TIME.get(succ_task_type, DEFAULT_HEURISTIC)}")
            return HEURISTIC_TIME.get(succ_task_type, DEFAULT_HEURISTIC)
        else:
            return est_time