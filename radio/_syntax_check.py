import ast
with open(r'C:\Dev\DeeJAI\radio\deejay_api.py', encoding='utf-8') as f:
    src = f.read()
try:
    ast.parse(src)
    print('syntax OK')
except SyntaxError as e:
    print(f'SyntaxError at line {e.lineno}: {e.msg}')
    print(f'  {e.text}')
