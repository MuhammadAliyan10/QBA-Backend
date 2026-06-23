import os

readme_content = """# {folder_name}

This directory is an integral part of the Quanta Execution Plane architecture.
Its exact execution role is to organize and provide the module logic for `{folder_name}`.
"""

def process_dir(root_dir):
    for root, dirs, files in os.walk(root_dir):
        if '__pycache__' in root or '.quanta_test_cache' in root:
            continue
        readme_path = os.path.join(root, "README.md")
        if not os.path.exists(readme_path):
            folder_name = os.path.basename(root)
            if not folder_name:
                folder_name = "src"
            with open(readme_path, "w") as f:
                f.write(readme_content.format(folder_name=folder_name))

if __name__ == "__main__":
    process_dir("apps/execution-plane/src")
