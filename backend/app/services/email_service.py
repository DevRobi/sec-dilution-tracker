
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import logging

logger = logging.getLogger(__name__)

def send_dilution_alert(user_email, ticker, offering_data):
    """
    Sends an email alert about a new dilution event.
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender_email = os.getenv("ALERT_FROM_EMAIL", smtp_user)

    if not smtp_user or not smtp_password:
        logger.warning("SMTP credentials not set. Skipping email alert.")
        return False

    subject = f"🔴 Dilution Alert: {ticker}"
    
    amount_display = offering_data.get('amount')
    if amount_display:
        if amount_display >= 1_000_000_000:
            amount_display = f"${amount_display/1_000_000_000:.1f} Billion"
        elif amount_display >= 1_000_000:
             amount_display = f"${amount_display/1_000_000:.1f} Million"
    else:
        amount_display = offering_data.get('amount_text', 'Unknown Amount')

    body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 8px;">
          <h2 style="color: #d32f2f;">Dilution Event Detected: {ticker}</h2>
          <p>A new potential dilution event has been detected in a recent SEC filing.</p>
          
          <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr style="background-color: #f5f5f5;">
              <td style="padding: 10px; border: 1px solid #ddd;"><strong>Type</strong></td>
              <td style="padding: 10px; border: 1px solid #ddd;">{offering_data.get('category')}</td>
            </tr>
            <tr>
              <td style="padding: 10px; border: 1px solid #ddd;"><strong>Date</strong></td>
              <td style="padding: 10px; border: 1px solid #ddd;">{offering_data.get('date')}</td>
            </tr>
            <tr style="background-color: #f5f5f5;">
               <td style="padding: 10px; border: 1px solid #ddd;"><strong>Amount</strong></td>
               <td style="padding: 10px; border: 1px solid #ddd; color: #d32f2f; font-weight: bold;">{amount_display}</td>
            </tr>
            <tr>
               <td style="padding: 10px; border: 1px solid #ddd;"><strong>Source</strong></td>
               <td style="padding: 10px; border: 1px solid #ddd;">
                 <a href="{offering_data.get('source_url', '#')}" style="color: #1976d2;">{offering_data.get('source_filing')}</a>
               </td>
            </tr>
          </table>
          
          <div style="background-color: #fff3cd; padding: 10px; border-left: 4px solid #ffc107; margin-top: 20px;">
            <strong>Snippet:</strong><br/>
            <em style="font-size: 0.9em;">"{offering_data.get('text', '')[:300]}..."</em>
          </div>
          
          <p style="margin-top: 30px; font-size: 0.8em; color: #777;">
            You are receiving this because {ticker} is in your Share Buyback Tracker portfolio.
          </p>
        </div>
      </body>
    </html>
    """

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = user_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(sender_email, user_email, msg.as_string())
        server.quit()
        logger.info(f"Email alert sent to {user_email} for {ticker}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")
        return False
