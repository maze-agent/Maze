import time
import datetime
import psutil
import pynvml
import csv
import os
import argparse
import socket
import pytz

def initialize(log_file_name):
    """Initializes NVML, creates the CSV file, and writes the header."""
    gpu_count = 0
    try:
        pynvml.nvmlInit()
        gpu_count = pynvml.nvmlDeviceGetCount()
        print(f"INFO: Found {gpu_count} GPUs.")
    except pynvml.NVMLError as e:
        print(f"WARNING: Could not initialize NVML. GPU data will not be recorded. Error: {e}")

    header = ["timestamp", "cpu_percent", "ram_percent"]
    
    # MODIFIED: If GPUs exist, add columns for average stats, not individual stats.
    if gpu_count > 0:
        header.extend(["gpu_avg_util_percent", "gpu_avg_mem_percent"])
    header.extend(["disk_read_MBps", "disk_write_MBps", "disk_total_MBps", "net_sent_MBps", "net_recv_MBps", "net_total_MBps"])

    with open(log_file_name, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
    
    print(f"INFO: Log file will be written to: {os.path.abspath(log_file_name)}")
    return gpu_count

def main_loop(gpu_count, log_file_name, interval):
    """The main monitoring loop."""
    
    last_disk_io = psutil.disk_io_counters()
    last_net_io = psutil.net_io_counters() 

    
    # Warm-up period for a stable first disk IO reading

    print("INFO: Warming up for 1 second to get a stable disk IO baseline...")
    time.sleep(interval)
    last_disk_io = psutil.disk_io_counters()
    last_net_io = psutil.net_io_counters() 
    last_time = time.time()
    cst_tz = pytz.timezone('Asia/Shanghai')

    print(f"INFO: Starting resource monitoring at {interval} second intervals...")
    print("INFO: You can run your experiment now. Press Ctrl+C to stop recording.")

    try:
        with open(log_file_name, 'a', newline='') as f:
            writer = csv.writer(f)
            while True:
                time.sleep(interval)

                # --- Data Collection ---
                utc_now = datetime.datetime.now(pytz.utc)
                cst_now = utc_now.astimezone(cst_tz)
                now_str = cst_now.strftime('%Y-%m-%d %H:%M:%S')
                
                cpu_percent = psutil.cpu_percent(interval=None)
                ram_percent = psutil.virtual_memory().percent
                row_data = [now_str, cpu_percent, ram_percent]

                # MODIFIED: Collect GPU data and calculate averages
                if gpu_count > 0:
                    util_rates = []
                    mem_percents = []
                    for i in range(gpu_count):
                        try:
                            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            
                            util_rates.append(util.gpu)
                            mem_percents.append((mem.used / mem.total) * 100)
                        except pynvml.NVMLError as e:
                            print(f"WARNING: Could not get data for GPU {i}. It will be excluded from averages for this interval. Error: {e}")
                    
                    if util_rates: # Check if any GPU data was successfully collected
                        avg_util = sum(util_rates) / len(util_rates)
                        avg_mem = sum(mem_percents) / len(mem_percents)
                        row_data.extend([round(avg_util, 2), round(avg_mem, 2)])
                    else:
                        row_data.extend(['N/A', 'N/A']) # If all GPUs failed
                
                # Collect Disk IO data

                current_time = time.time()
                delta_time = current_time - last_time

                current_disk_io = psutil.disk_io_counters()
                disk_delta_read = current_disk_io.read_bytes - last_disk_io.read_bytes
                disk_delta_write = current_disk_io.write_bytes - last_disk_io.write_bytes
                disk_read_MBps = disk_delta_read / (1024 * 1024 * delta_time)
                disk_write_MBps = disk_delta_write / (1024 * 1024 * delta_time)
                disk_total_MBps = disk_read_MBps + disk_write_MBps

                current_net_io = psutil.net_io_counters()
                net_delta_sent = current_net_io.bytes_sent - last_net_io.bytes_sent
                net_delta_recv = current_net_io.bytes_recv - last_net_io.bytes_recv
                net_sent_MBps = (net_delta_sent / (1024 * 1024)) / delta_time if delta_time > 0 else 0
                net_recv_MBps = (net_delta_recv / (1024 * 1024)) / delta_time if delta_time > 0 else 0
                net_total_MBps= net_sent_MBps+ net_recv_MBps
                row_data.extend([round(disk_read_MBps, 2), round(disk_write_MBps, 2), round(disk_total_MBps, 2), round(net_sent_MBps, 2), round(net_recv_MBps, 2), round(net_total_MBps, 2)])
                last_disk_io = current_disk_io
                last_net_io = current_net_io
                last_time = current_time
                # --- Write to file ---
                writer.writerow(row_data)
                f.flush()

    except KeyboardInterrupt:
        print("\nINFO: Script manually stopped.")
    except Exception as e:
        print(f"ERROR: An unexpected error occurred: {e}")
    finally:
        if gpu_count > 0:
            pynvml.nvmlShutdown()
        print("INFO: Monitoring stopped.")

def get_local_ip():
    """Gets the primary non-loopback IP address of the local machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="System Resource Monitoring Script (Robust Version)")
    parser.add_argument("-i", "--interval", type=int, default=1, help="Sampling interval in seconds.")
    parser.add_argument("-o", "--output", type=str, default=None, help="Output CSV file name.")
    parser.add_argument("-s", "--start_time", type=str, default="2025-07-27 16:00:00", help="Scheduled start time in 'YYYY-MM-DD HH:MM:SS' format (local time).")
    args = parser.parse_args()
    
    # Generate default filename if not provided
    if args.output is None:
        ip = get_local_ip().replace(".", "_")
        cst_tz = pytz.timezone('Asia/Shanghai')
        timestamp = datetime.datetime.now(cst_tz).strftime("%Y%m%d_%H%M%S")
        args.output = f"resource_stats_{ip}_{timestamp}.csv"

    # Handle scheduled start time
    if args.start_time:
        try:
            cst_tz = pytz.timezone('Asia/Shanghai')
            start_dt_naive = datetime.datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")
            start_dt_aware = cst_tz.localize(start_dt_naive)
            now_dt_aware = datetime.datetime.now(cst_tz)
            
            if start_dt_aware > now_dt_aware:
                wait_seconds = (start_dt_aware - now_dt_aware).total_seconds()
                print(f"INFO: Scheduled start set. Monitoring will begin in {int(wait_seconds // 3600)}h {int((wait_seconds % 3600) // 60)}m {int(wait_seconds % 60)}s...")
                time.sleep(wait_seconds)
            else:
                print("WARNING: The specified start time is in the past. Starting immediately.")
        except ValueError:
            print("ERROR: Incorrect start_time format. Please use 'YYYY-MM-DD HH:MM:SS'.")
            exit(1)

    # Prime cpu_percent to avoid 0.0 reading on first loop
    psutil.cpu_percent(interval=None) 
    
    # Start the monitoring
    num_gpus = initialize(args.output)
    main_loop(num_gpus, args.output, args.interval)