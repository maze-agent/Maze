for i in {1..5}; do
    python3 api_test.py --id $i &
done
wait  # 可选：等待所有后台任务完成