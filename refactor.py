import os
import re

def to_camel_case(snake_str):
    if not snake_str or '_' not in snake_str:
        if '-' in snake_str:
            components = snake_str.split('-')
            return components[0] + ''.join(x.title() for x in components[1:])
        return snake_str
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def process_dir(root_dir):
    for root, dirs, files in os.walk(root_dir, topdown=False):
        for name in files:
            if name.endswith('.py') and name != '__init__.py':
                base_name = name[:-3]
                camel_name = to_camel_case(base_name) + '.py'
                if camel_name != name:
                    os.rename(os.path.join(root, name), os.path.join(root, camel_name))
        
        for name in dirs:
            if name != '__pycache__' and not name.startswith('.'):
                camel_name = to_camel_case(name)
                if camel_name != name:
                    os.rename(os.path.join(root, name), os.path.join(root, camel_name))

if __name__ == "__main__":
    process_dir("apps/execution-plane/src")
