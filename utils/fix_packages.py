import os

def add_init_files(root_dir):
    for root, dirs, files in os.walk(root_dir):
        # Ignora venv e git
        if ".venv" in root or ".git" in root or "__pycache__" in root:
            continue
            
        if "__init__.py" not in files:
            init_path = os.path.join(root, "__init__.py")
            print(f"Criando {init_path}")
            with open(init_path, "w") as f:
                f.write("# Package marker\n")

if __name__ == "__main__":
    add_init_files(".")
