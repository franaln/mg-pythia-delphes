from importlib.resources import files
from pathlib import Path
import string

# TEMPLATES_DIR =

def _resource(relative):
    result = files('mgpd').joinpath('data')
    for part in Path(relative).parts:
        result = result.joinpath(part)
    return result

def load_template(name):
    return _resource(f'templates/{name}').read_text(encoding='utf-8')

# def replace_template(template_text, replace_dict):
#     template = string.Template(template_text)
#     return template.substitute(replace_dict)

# def load_replace_template(name, replace_dict):
#     template = string.Template(load_template(name))
#     return template.substitute(replace_dict)

def create_file_from_template(name, output_path, replace_dict={}):
    template_text = load_template(name)
    if replace_dict:
        template = string.Template(template_text)
        template_text = template.substitute(replace_dict)

    with open(output_path, 'w') as f:
        f.write(template_text)

def create_file_from_script_template(name, output_path, replace_dict={}):
    """
    Special replacement for bash script to avoid ${} confusion.
    Not safe at all. Use with care, only one replacement is safer.
    """
    template_text = load_template(name)
    if replace_dict:
        for old_text, new_text in replace_dict.items():
            template_text = template_text.replace(old_text, new_text)

    with open(output_path, 'w') as f:
        f.write(template_text)