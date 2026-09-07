import sys
import json
import asyncio
import pdfplumber
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QLineEdit, QPushButton, QTextEdit, QLabel, QFileDialog)
from PySide6.QtCore import QThread, Signal
from playwright.async_api import async_playwright
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# ---------------------------------------------------------
# 1. LLM Structured Output Schema
# ---------------------------------------------------------
class FormAction(BaseModel):
    autofill_id: str = Field(description="The data-autofill-id of the element")
    action_type: str = Field(description="Must be 'fill', 'select', or 'check'")
    value: str = Field(description="The exact text to type, or the exact option value to select")

class FormMapping(BaseModel):
    actions: list[FormAction]

# ---------------------------------------------------------
# 2. Background Automation Thread
# ---------------------------------------------------------
class AutofillThread(QThread):
    log_msg = Signal(str)

    def __init__(self, url, resume_path):
        super().__init__()
        self.url = url
        self.resume_path = resume_path
        self.api_key = "YOUR_GOOGLE_API_KEY" # Replace or use os.environ.get("GOOGLE_API_KEY")

    def run(self):
        # Run the async Playwright loop inside the QThread
        asyncio.run(self.run_automation())

    async def run_automation(self):
        self.log_msg.emit("Starting parsing...")
        resume_text = self.extract_resume_text()
        if not resume_text:
            return

        self.log_msg.emit("Launching browser (Headless=False to allow CAPTCHA handling)...")
        async with async_playwright() as p:
            # We use headless=False so you can step in for CAPTCHAs or logins
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                self.log_msg.emit(f"Navigating to {self.url}...")
                await page.goto(self.url, wait_until="networkidle")
                
                # Handling Multi-Page Applications (Workday style loop)
                page_count = 1
                while True:
                    self.log_msg.emit(f"--- Processing Page {page_count} ---")
                    
                    # 1. Extract DOM Schema via Javascript Injection
                    # This assigns custom IDs to inputs to bypass fragile CSS selectors
                    schema = await self.extract_dom_schema(page)
                    self.log_msg.emit(f"Found {len(schema)} fillable fields.")

                    if not schema:
                        self.log_msg.emit("No fields found or reached end of application.")
                        break

                    # 2. Map data with LLM
                    self.log_msg.emit("Asking LLM to map resume data to fields...")
                    mapping = self.get_llm_mapping(schema, resume_text)
                    
                    # 3. Execute filling
                    self.log_msg.emit("Executing form fill...")
                    await self.fill_form(page, mapping)

                    # 4. Look for 'Next' or 'Continue' button to handle pagination
                    next_button = await self.find_next_button(page)
                    if next_button:
                        self.log_msg.emit("Clicking 'Next Page'...")
                        await next_button.click()
                        await page.wait_for_load_state("networkidle")
                        page_count += 1
                    else:
                        self.log_msg.emit("No 'Next' button found. Awaiting your review before submitting!")
                        # Pause indefinitely so you can review and click Submit yourself
                        await page.pause()
                        break

            except Exception as e:
                self.log_msg.emit(f"Error: {str(e)}")
            finally:
                await browser.close()

    def extract_resume_text(self):
        try:
            text = ""
            with pdfplumber.open(self.resume_path) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() + "\n"
            return text
        except Exception as e:
            self.log_msg.emit(f"Failed to read PDF: {e}")
            return None

    async def extract_dom_schema(self, page):
        # We inject JS to tag every input with a custom 'data-autofill-id'. 
        # This solves the problem of shadow DOMs and dynamically generated React IDs.
        js_script = """
        () => {
            let elements = document.querySelectorAll('input:not([type="hidden"]), select, textarea');
            let schema = [];
            elements.forEach((el, index) => {
                let uniqueId = 'autofill_' + Date.now() + '_' + index;
                el.setAttribute('data-autofill-id', uniqueId);
                
                let data = {
                    autofill_id: uniqueId,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || el.id || '',
                    aria_label: el.getAttribute('aria-label') || '',
                };
                
                // Handle Dropdowns
                if (el.tagName === 'SELECT') {
                    data.options = Array.from(el.options).map(o => o.value).filter(v => v);
                }
                
                // Attempt to find nearby label text
                let label = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
                if (label) data.label_text = label.innerText.trim();
                
                schema.push(data);
            });
            return schema;
        }
        """
        # Run across main page and any iFrames (handles Greenhouse embedded forms)
        schema = []
        for frame in page.frames:
            try:
                frame_schema = await frame.evaluate(js_script)
                schema.extend(frame_schema)
            except:
                continue # Ignore cross-origin frame access errors
        return schema

    def get_llm_mapping(self, form_schema, resume_text):
        client = genai.Client(api_key=self.api_key)
        
        prompt = f"""
        You are an AI assistant filling out a job application.
        Match the user's resume data to the provided HTML form schema.
        
        Rules:
        - For text/textarea fields, action_type is "fill", value is the text to type.
        - For select fields, action_type is "select", value MUST be one of the provided options.
        - For checkboxes/radios, action_type is "check", value is "true".
        - Leave fields blank if the resume doesn't contain the answer (e.g., race/gender/veteran status).
        
        Resume:
        {resume_text}
        
        Form Schema:
        {json.dumps(form_schema, indent=2)}
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FormMapping,
                temperature=0.1 # Low temp for deterministic mapping
            ),
        )
        return json.loads(response.text)

    async def fill_form(self, page, mapping):
        for action in mapping.get('actions', []):
            selector = f"[data-autofill-id='{action['autofill_id']}']"
            try:
                # Search all frames for the tagged element
                for frame in page.frames:
                    locator = frame.locator(selector)
                    if await locator.count() > 0:
                        if action['action_type'] == 'fill':
                            await locator.fill(action['value'])
                        elif action['action_type'] == 'select':
                            await locator.select_option(value=action['value'])
                        elif action['action_type'] == 'check':
                            await locator.check()
                        self.log_msg.emit(f"Filled {action['autofill_id']} with '{action['value']}'")
                        break
            except Exception as e:
                self.log_msg.emit(f"Failed to interact with {action['autofill_id']}: {e}")

    async def find_next_button(self, page):
        # Common text for multi-page application routing
        for text in ["Next", "Continue", "Next Step", "Save and Continue"]:
            btn = page.get_by_role("button", name=text, exact=False)
            if await btn.count() > 0 and await btn.first.is_visible():
                return btn.first
        return None

# ---------------------------------------------------------
# 3. PySide6 Desktop Dashboard
# ---------------------------------------------------------
class JobAppDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto-Apply Assistant")
        self.resize(600, 400)
        self.resume_path = None

        layout = QVBoxLayout()

        # Resume Selection
        res_layout = QHBoxLayout()
        self.res_label = QLabel("No Resume Selected")
        btn_res = QPushButton("Select Resume PDF")
        btn_res.clicked.connect(self.select_resume)
        res_layout.addWidget(self.res_label)
        res_layout.addWidget(btn_res)
        layout.addLayout(res_layout)

        # URL Input
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste job application URL here...")
        layout.addWidget(self.url_input)

        # Start Button
        self.btn_start = QPushButton("Start Automation")
        self.btn_start.clicked.connect(self.start_automation)
        self.btn_start.setStyleSheet("background-color: #2E8B57; color: white; font-weight: bold; padding: 8px;")
        layout.addWidget(self.btn_start)

        # Log Console
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        layout.addWidget(self.console)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def select_resume(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select Resume", "", "PDF Files (*.pdf)")
        if file:
            self.resume_path = file
            self.res_label.setText(file.split('/')[-1])

    def start_automation(self):
        url = self.url_input.text().strip()
        if not url or not self.resume_path:
            self.log_message("Error: Please select a resume and enter a URL.")
            return

        self.btn_start.setEnabled(False)
        self.log_message("Starting application process...")
        
        self.thread = AutofillThread(url, self.resume_path)
        self.thread.log_msg.connect(self.log_message)
        self.thread.finished.connect(lambda: self.btn_start.setEnabled(True))
        self.thread.start()

    def log_message(self, msg):
        self.console.append(msg)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = JobAppDashboard()
    window.show()
    sys.exit(app.exec())
