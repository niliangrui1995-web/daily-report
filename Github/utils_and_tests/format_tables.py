import re
import io
import os

file_path = r'd:\vcp_hunter\每日热点汇总\Github\VCP_北美战报系统综合开发文档.md'

def get_char_width(c):
    if '\u4e00' <= c <= '\u9fa5' or '\u3000' <= c <= '\u303f' or '\uff00' <= c <= '\uffef':
        return 2
    return 1

def string_width(s):
    return sum(get_char_width(c) for c in s)

if not os.path.exists(file_path):
    print("File not found")
    exit(1)

with io.open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i].rstrip('\r\n')
    if line.startswith('|') and line.endswith('|') and i + 1 < len(lines):
        next_line = lines[i+1].rstrip('\r\n')
        # Check if next_line is a markdown table separator
        if next_line.startswith('|') and next_line.endswith('|'):
            inner = next_line[1:-1].replace('|', '').replace('-', '').replace(':', '').replace(' ', '')
            if inner == '':
                # It is a table
                table_lines = []
                while i < len(lines):
                    r_line = lines[i].rstrip('\r\n')
                    if r_line.startswith('|') and r_line.endswith('|'):
                        table_lines.append(r_line)
                        i += 1
                    else:
                        break
                
                rows = []
                for row_line in table_lines:
                    cells = [c.strip() for c in row_line.strip('|').split('|')]
                    rows.append(cells)
                
                # compute max width per col
                cols = len(rows[0])
                col_widths = [3] * cols
                for r_idx, row in enumerate(rows):
                    if r_idx == 1: continue
                    for c_idx, cell in enumerate(row):
                        if c_idx < cols:
                            col_widths[c_idx] = max(col_widths[c_idx], string_width(cell))
                
                for r_idx, row in enumerate(rows):
                    formatted_cells = []
                    for c_idx in range(cols):
                        cell = row[c_idx] if c_idx < len(row) else ''
                        width = col_widths[c_idx]
                        if r_idx == 1:
                            formatted_cells.append('-' * width)
                        else:
                            pad = width - string_width(cell)
                            formatted_cells.append(cell + ' ' * pad)
                    new_lines.append('| ' + ' | '.join(formatted_cells) + ' |')
                
                continue # loop processed table
    new_lines.append(line)
    i += 1

with io.open(file_path, 'w', encoding='utf-8') as f:
    for line in new_lines:
        f.write(line + '\n')

print("Formatted Markdown tables.")
