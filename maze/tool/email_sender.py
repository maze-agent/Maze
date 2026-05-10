from maze import task
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart



@task
def email_sender(smtp_server: str = "", port: int = 587, username: str = "", password: str = "", to_email: str = "", subject: str = "", body: str = "", use_tls: bool = True):
    
    required_params = [smtp_server, username, password, to_email]
    if not all(required_params):
        return {"result": None, "error": "Missing required parameters: smtp_server, username, password, or to_email"}
    
    try:
        msg = MIMEMultipart()
        msg['From'] = username
        msg['To'] = to_email
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(smtp_server, port)
        if use_tls:
            server.starttls()
        server.login(username, password)
        
        text = msg.as_string()
        server.sendmail(username, to_email, text)
        server.quit()
        
        return {"result": f"Email sent successfully to {to_email}"}
    except Exception as e:
        return {"result": None, "error": str(e)}