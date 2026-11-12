"""Browser acceptance of the real library API and PDF.js reader (isolated test data)."""
import argparse
import io
import os
from pathlib import Path
import time

import fitz
import requests
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    key = os.environ['DESKTOP_TEST_KEY']
    headers = {'X-API-Key': key}
    pdf = fitz.open()
    first = pdf.new_page()
    first.insert_text((60, 90), 'Reading with traceable evidence', fontsize=24)
    first.insert_text((60, 140), 'Each annotation links a passage to its page.', fontsize=14)
    first.insert_text((60, 170), 'Keep source evidence and interpretation separate.', fontsize=14)
    pdf.new_page().insert_text((60, 90), 'Second page: testing reading progress.', fontsize=18)
    name = 'desktop-acceptance.pdf'
    for previous in requests.get(args.url+'/api/v1/library',headers=headers,timeout=15).json()['documents']:
        if previous['name'] == name:
            requests.patch(args.url+f"/api/v1/library/documents/{previous['id']}",headers=headers,json={'trashed':True},timeout=15).raise_for_status()
    result = requests.post(args.url+'/api/v1/library/documents', headers=headers,
                           files={'file': (name, pdf.tobytes(), 'application/pdf')}, timeout=15)
    result.raise_for_status()
    identifier = result.json()['id']
    out = Path(__file__).resolve().parents[1]/'.visual_check'
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script('sessionStorage.setItem("research-agent.api-token", '+__import__('json').dumps(key)+');')
        page.goto(args.url)
        page.get_by_role('button', name='论文库', exact=True).click()
        page.get_by_role('button', name=name, exact=True).click()
        page.locator('.pdf-sheet .textLayer span').first.wait_for()
        assert page.locator('.pdf-sheet canvas').evaluate('(el) => el.width') > 100
        page.get_by_role('spinbutton', name='缩放比例', exact=True).fill('150')
        page.get_by_role('spinbutton', name='缩放比例', exact=True).blur()
        page.wait_for_timeout(800)
        assert page.get_by_role('spinbutton', name='缩放比例', exact=True).input_value() == '150'
        page.get_by_role('button', name='适应宽度', exact=True).click()
        page.wait_for_timeout(500)
        page.locator('.textLayer').evaluate('''(el) => {
            const span=Array.from(el.querySelectorAll('span')).find(s=>s.textContent.includes('Each annotation'));
            const range=document.createRange();range.selectNodeContents(span);
            const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            el.dispatchEvent(new KeyboardEvent('keyup',{key:'Shift',bubbles:true}));
        }''')
        page.get_by_role('button', name='添加批注', exact=True).click()
        page.get_by_role('textbox', name='笔记正文', exact=True).fill('持久化批注：核对原文证据。')
        page.get_by_role('button', name='保存笔记', exact=True).click()
        page.locator('.note-save-status').filter(has_text='已保存').wait_for()
        saved = requests.get(args.url+f'/api/v1/library/documents/{identifier}/notes',headers=headers).json()
        assert any(n['content']=='持久化批注：核对原文证据。' and n['anchor']['rects'] for n in saved),saved
        page.get_by_role('button', name='下一页', exact=True).click()
        page.wait_for_timeout(700)
        page.get_by_role('button', name='设置', exact=True).click()
        page.get_by_role('button', name='每日检索', exact=True).click()
        page.get_by_role('button', name='用户画像', exact=True).click()
        page.get_by_role('tab', name=name, exact=True).click()
        assert page.get_by_role('spinbutton', name='页码', exact=True).input_value()=='2'
        page.get_by_role('button', name='上一页', exact=True).click()
        page.wait_for_timeout(700)
        page.screenshot(path=str(out/'desktop-reader.png'),full_page=True)
        page.get_by_role('tab', name='论文库', exact=True).click()
        page.get_by_role('button', name='新建文件夹', exact=True).click()
        folder_name='验收文件夹 '+str(int(time.time()))
        page.get_by_role('textbox', name='新文件夹名称', exact=True).fill(folder_name)
        page.get_by_role('button', name='确定', exact=True).click()
        page.get_by_role('button',name='▱ '+folder_name,exact=True).wait_for()
        page.get_by_role('checkbox',name='选择 '+name,exact=True).check()
        page.get_by_role('button',name='移动到',exact=True).click()
        page.get_by_role('combobox',name='目标文件夹',exact=True).select_option(label=folder_name)
        page.get_by_role('button',name='确定',exact=True).click()
        page.wait_for_timeout(500)
        page.screenshot(path=str(out/'desktop-library.png'),full_page=True)
        for width in [1440,1024,800]:
            page.set_viewport_size({'width':width,'height':900})
            page.wait_for_timeout(100)
            bounds=page.evaluate('({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})')
            assert bounds['scroll']<=bounds['width']+1,bounds
        assert not errors,errors
        browser.close()
    print('Real PDF render, zoom, anchored note persistence, tabs, settings, folders and file move passed.')


if __name__ == '__main__':
    main()
