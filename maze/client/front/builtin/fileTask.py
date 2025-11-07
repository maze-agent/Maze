"""
Built-in file processing task examples

These tasks demonstrate how to handle file types like images and audio
"""

from maze.client.front.decorator import task


@task(
    inputs=["image_path"],
    outputs=["info"],
    data_types={
        "image_path": "file:image",
        "info": "dict"
    },
    resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0}
)
def get_image_info(params):
    """
    Get image information
    
    Input:
        image_path: Image file path (automatically uploaded to server)
        
    Output:
        info: Image information (dimensions, format, etc.)
    """
    from PIL import Image
    import os
    
    image_path = params.get("image_path")
    
    # 打开图片
    img = Image.open(image_path)
    
    # 获取信息
    info = {
        "width": img.width,
        "height": img.height,
        "format": img.format,
        "mode": img.mode,
        "size_bytes": os.path.getsize(image_path),
        "path": image_path
    }
    
    return {"info": info}


@task(
    inputs=["image_path", "output_size"],
    outputs=["resized_image_path", "info"],
    data_types={
        "image_path": "file:image",
        "output_size": "str",  # 格式: "宽x高", 如 "800x600"
        "resized_image_path": "file:image",
        "info": "dict"
    },
    resources={"cpu": 1, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0}
)
def resize_image(params):
    """
    Resize image
    
    Input:
        image_path: Input image path
        output_size: Output size in format "widthxheight"
        
    Output:
        resized_image_path: Resized image path
        info: Processing information
    """
    from PIL import Image
    import os
    from pathlib import Path
    
    image_path = params.get("image_path")
    output_size_str = params.get("output_size")
    
    # Parse target size
    width, height = map(int, output_size_str.split("x"))
    
    # Open and resize image
    img = Image.open(image_path)
    original_size = img.size
    
    resized_img = img.resize((width, height), Image.Resampling.LANCZOS)
    
    # Generate output path (in same directory)
    input_path = Path(image_path)
    output_path = input_path.parent / f"resized_{input_path.name}"
    
    # Save
    resized_img.save(output_path, quality=95)
    
    info = {
        "original_size": f"{original_size[0]}x{original_size[1]}",
        "new_size": f"{width}x{height}",
        "input_path": str(image_path),
        "output_path": str(output_path)
    }
    
    return {
        "resized_image_path": str(output_path),
        "info": info
    }


@task(
    inputs=["image_path"],
    outputs=["grayscale_image_path"],
    data_types={
        "image_path": "file:image",
        "grayscale_image_path": "file:image"
    },
    resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0}
)
def convert_to_grayscale(params):
    """
    Convert image to grayscale
    
    Input:
        image_path: Input image path
        
    Output:
        grayscale_image_path: Grayscale image path
    """
    from PIL import Image
    from pathlib import Path
    
    image_path = params.get("image_path")
    
    # Open and convert
    img = Image.open(image_path)
    grayscale_img = img.convert('L')
    
    # Generate output path
    input_path = Path(image_path)
    output_path = input_path.parent / f"gray_{input_path.name}"
    
    # Save
    grayscale_img.save(output_path)
    
    return {"grayscale_image_path": str(output_path)}

