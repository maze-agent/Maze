"""
Built-in file processing task examples

These tasks demonstrate how to handle file types like images and audio
"""

from maze.client.front.decorator import task


@task(data_types={"image_path": "file:image", "info": "dict"}, resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0})
def get_image_info(image_path: str):
    """
    Get image information

    Input:
        image_path: Image file path (automatically uploaded to server)

    Output:
        info: Image information (dimensions, format, etc.)
    """
    from PIL import Image
    import os

    img = Image.open(image_path)

    info = {
        "width": img.width,
        "height": img.height,
        "format": img.format,
        "mode": img.mode,
        "size_bytes": os.path.getsize(image_path),
        "path": image_path,
    }

    return {"info": info}


@task(data_types={"image_path": "file:image", "output_size": "str", "resized_image_path": "file:image", "info": "dict"}, resources={"cpu": 1, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0})
def resize_image(image_path: str, output_size: str):
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
    from pathlib import Path

    width, height = map(int, output_size.split("x"))

    img = Image.open(image_path)
    original_size = img.size

    resized_img = img.resize((width, height), Image.Resampling.LANCZOS)

    input_path = Path(image_path)
    output_path = input_path.parent / f"resized_{input_path.name}"

    resized_img.save(output_path, quality=95)

    info = {
        "original_size": f"{original_size[0]}x{original_size[1]}",
        "new_size": f"{width}x{height}",
        "input_path": str(image_path),
        "output_path": str(output_path),
    }

    return {
        "resized_image_path": str(output_path),
        "info": info,
    }


@task(data_types={"image_path": "file:image", "grayscale_image_path": "file:image"}, resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0})
def convert_to_grayscale(image_path: str):
    """
    Convert image to grayscale

    Input:
        image_path: Input image path

    Output:
        grayscale_image_path: Grayscale image path
    """
    from PIL import Image
    from pathlib import Path

    img = Image.open(image_path)
    grayscale_img = img.convert("L")

    input_path = Path(image_path)
    output_path = input_path.parent / f"gray_{input_path.name}"

    grayscale_img.save(output_path)

    return {"grayscale_image_path": str(output_path)}
