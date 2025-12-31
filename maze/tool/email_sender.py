from maze import task
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


@task(
    inputs=["smtp_server", "port", "username", "password", "to_email", "subject", "body", "use_tls"],
    outputs=["result"],
)
def email_sender(params):
    smtp_server = params.get("smtp_server")
    port = params.get("port", 587)
    username = params.get("username")
    password = params.get("password")
    to_email = params.get("to_email")
    subject = params.get("subject", "")
    body = params.get("body", "")
    use_tls = params.get("use_tls", True)
    
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