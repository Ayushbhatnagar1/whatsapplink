import os
import re
import requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, jsonify
from twilio.rest import Client
import openai
from bs4 import BeautifulSoup
import urllib.parse
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

class WhatsAppLinkLogger:
    def __init__(self):
        # Twilio Configuration
        self.twilio_client = Client(
            os.environ.get('TWILIO_ACCOUNT_SID'),
            os.environ.get('TWILIO_AUTH_TOKEN')
        )
        self.twilio_phone_number = os.environ.get('TWILIO_PHONE_NUMBER')
        
        # OpenAI Configuration (using free tier)
        openai.api_key = os.environ.get('OPENAI_API_KEY')
        
        # Google Sheets Configuration
        self.setup_google_sheets()
        
        # URL pattern for link detection
        self.url_pattern = re.compile(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        )
    
    def setup_google_sheets(self):
        """Setup Google Sheets connection"""
        try:
            # Google Sheets scope
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
            
            # Load credentials from environment or service account file
            creds_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
            if creds_json:
                import json
                creds_dict = json.loads(creds_json)
                creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            else:
                creds = Credentials.from_service_account_file(
                    'path/to/your/service-account-key.json', 
                    scopes=scope
                )
            
            self.gc = gspread.authorize(creds)
            
            # Open or create spreadsheet
            spreadsheet_name = os.environ.get('SPREADSHEET_NAME', 'WhatsApp Link Logger')
            try:
                self.sheet = self.gc.open(spreadsheet_name).sheet1
            except gspread.SpreadsheetNotFound:
                # Create new spreadsheet
                spreadsheet = self.gc.create(spreadsheet_name)
                spreadsheet.share(os.environ.get('YOUR_EMAIL'), perm_type='user', role='writer')
                self.sheet = spreadsheet.sheet1
                
                # Add headers
                headers = ['Date', 'Time', 'Message Type', 'Content', 'URL', 'Summary', 'Sender']
                self.sheet.append_row(headers)
                
        except Exception as e:
            logger.error(f"Error setting up Google Sheets: {e}")
            self.sheet = None
    
    def extract_page_title(self, url):
        """Extract title from webpage"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                title = soup.find('title')
                if title:
                    return title.get_text().strip()[:100]  # Limit title length
            return None
        except Exception as e:
            logger.error(f"Error extracting title from {url}: {e}")
            return None
    
    def generate_summary_with_ollama(self, content, url=None):
        """Generate summary using Ollama (free local LLM)"""
        try:
            # Ollama API endpoint (assuming it's running locally)
            ollama_url = "http://localhost:11434/api/generate"
            
            if url:
                page_title = self.extract_page_title(url)
                prompt = f"Summarize this URL and content in exactly 4-5 words: {url} - {page_title or ''} - {content[:200]}"
            else:
                prompt = f"Summarize this message in exactly 4-5 words: {content[:200]}"
            
            payload = {
                "model": "llama3.2:1b",  # Lightweight model
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 20
                }
            }
            
            response = requests.post(ollama_url, json=payload, timeout=30)
            if response.status_code == 200:
                result = response.json()
                summary = result.get('response', '').strip()
                # Clean and limit to 4-5 words
                words = summary.split()[:5]
                return ' '.join(words)
            
        except Exception as e:
            logger.error(f"Error with Ollama API: {e}")
        
        # Fallback to OpenAI GPT-3.5-turbo (has free tier)
        return self.generate_summary_with_openai(content, url)
    
    def generate_summary_with_openai(self, content, url=None):
        """Generate summary using OpenAI as fallback"""
        try:
            if url:
                page_title = self.extract_page_title(url)
                prompt = f"Summarize this URL in exactly 4-5 words: {url} - {page_title or ''}"
            else:
                prompt = f"Summarize this message in exactly 4-5 words: {content[:200]}"
            
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that creates very brief 4-5 word summaries."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=20,
                temperature=0.3
            )
            
            summary = response.choices[0].message.content.strip()
            words = summary.split()[:5]
            return ' '.join(words)
            
        except Exception as e:
            logger.error(f"Error with OpenAI API: {e}")
            return "Summary unavailable"
    
    def log_to_spreadsheet(self, message_type, content, url=None, summary="", sender=""):
        """Log message to Google Sheets"""
        if not self.sheet:
            logger.error("Google Sheets not configured")
            return
        
        try:
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")
            
            row = [
                date_str,
                time_str,
                message_type,
                content[:500],  # Limit content length
                url or "",
                summary,
                sender
            ]
            
            self.sheet.append_row(row)
            logger.info(f"Logged to spreadsheet: {message_type} - {summary}")
            
        except Exception as e:
            logger.error(f"Error logging to spreadsheet: {e}")
    
    def process_message(self, message_body, sender_number):
        """Process incoming WhatsApp message"""
        logger.info(f"Processing message from {sender_number}: {message_body}")
        
        # Extract URLs from message
        urls = self.url_pattern.findall(message_body)
        
        if urls:
            for url in urls:
                # Generate summary for each URL
                summary = self.generate_summary_with_ollama(message_body, url)
                
                # Log to spreadsheet
                self.log_to_spreadsheet(
                    message_type="Link",
                    content=message_body,
                    url=url,
                    summary=summary,
                    sender=sender_number
                )
                
            response_text = f"✅ Logged {len(urls)} link(s) to your spreadsheet!"
        else:
            # Regular message without links
            summary = self.generate_summary_with_ollama(message_body)
            
            # Log to spreadsheet
            self.log_to_spreadsheet(
                message_type="Message",
                content=message_body,
                summary=summary,
                sender=sender_number
            )
            
            response_text = "✅ Message logged to your spreadsheet!"
        
        return response_text
    
    def send_whatsapp_message(self, to_number, message):
        """Send WhatsApp message via Twilio"""
        try:
            message = self.twilio_client.messages.create(
                body=message,
                from_=f'whatsapp:{self.twilio_phone_number}',
                to=f'whatsapp:{to_number}'
            )
            logger.info(f"Sent message to {to_number}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")

# Initialize bot
bot = WhatsAppLinkLogger()

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for incoming WhatsApp messages"""
    try:
        # Get message data from Twilio
        message_body = request.form.get('Body', '')
        sender_number = request.form.get('From', '').replace('whatsapp:', '')
        
        if message_body and sender_number:
            # Process the message
            response_text = bot.process_message(message_body, sender_number)
            
            # Send confirmation back to user
            bot.send_whatsapp_message(sender_number, response_text)
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    # Set environment variables before running
    required_vars = [
        'TWILIO_ACCOUNT_SID',
        'TWILIO_AUTH_TOKEN', 
        'TWILIO_PHONE_NUMBER',
        'GOOGLE_SHEETS_CREDENTIALS',
        'SPREADSHEET_NAME'
    ]
    
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        logger.error(f"Missing environment variables: {missing_vars}")
        exit(1)
    
    # Run the Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)