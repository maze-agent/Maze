from maze import task
import mutagen

@task(
    inputs=["file_path"],
    outputs=["result"],
)
def mp3_reader(params):
    file_path = params.get("file_path")
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    
    try:
        audio_file = mutagen.mp3.MP3(file_path)
        
        info = {
            "length": audio_file.info.length, 
            "bitrate": audio_file.info.bitrate,  
            "sample_rate": audio_file.info.sample_rate,  
            "metadata": {}
        }
        
        if hasattr(audio_file, 'tags') and audio_file.tags:
            for key, value in audio_file.tags.items():
                info["metadata"][key] = str(value)
                
        return {"result": info}
    except Exception as e:
        return {"result": None, "error": str(e)}