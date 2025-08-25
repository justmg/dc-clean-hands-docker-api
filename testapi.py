#!/usr/bin/env python3
"""
Temporary API test with mock browser automation 
to verify the core functionality works while we fix browser paths
"""
import asyncio
import os
import httpx
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel, EmailStr, Field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="DC Clean Hands Checker - Test Mode")

class CleanHandsRequest(BaseModel):
    notice: str = Field(..., min_length=5, max_length=64)
    last4: str = Field(..., pattern=r"^\d{4}$")
    email: EmailStr

# Mock browser automation result for testing
async def mock_clean_hands_workflow(notice: str, last4: str, session_id: str):
    """Mock workflow for testing email functionality"""
    print(f"MOCK: Checking notice {notice} with last4 {last4}")
    
    # Simulate browser work
    await asyncio.sleep(2)
    
    # For your specific notice, return compliant status
    if notice == "L0014500721" and last4 == "0257":
        return {
            "status": "compliant",
            "message": "Certificate is compliant (MOCK RESULT)",
            "pdf_path": None,  # No PDF in mock mode
            "session_id": session_id
        }
    else:
        return {
            "status": "unknown", 
            "message": "Mock result for testing",
            "pdf_path": None,
            "session_id": session_id
        }

async def send_email_via_cloudmailin(to_email: str, subject: str, html_body: str, text_body: str, pdf_path=None):
    """Send email via CloudMailin SMTP API"""
    
    username = os.getenv("CLOUDMAILIN_SMTP_USERNAME")
    api_token = os.getenv("CLOUDMAILIN_API_TOKEN") 
    from_email = os.getenv("FROM_EMAIL", "noreply@example.com")
    from_name = os.getenv("FROM_NAME", "Clean Hands Bot")
    
    if not username or not api_token:
        print("Missing CloudMailin credentials - email not sent")
        return {"status": "error", "message": "Missing email credentials"}
    
    # CloudMailin SMTP API endpoint (not test mode)  
    url = "https://api.cloudmailin.com/api/v0.1/messages"
    
    # Prepare email data
    data = {
        "from": f"{from_name} <{from_email}>",
        "to": to_email,
        "subject": subject,
        "plain": text_body,
        "html": html_body
    }
    
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=data, headers=headers)
            
        if response.status_code == 200:
            print(f"Email sent successfully to {to_email}")
            return {"status": "success", "message": "Email sent"}
        else:
            print(f"Email failed: {response.status_code} - {response.text}")
            return {"status": "error", "message": f"Email API error: {response.status_code}"}
            
    except Exception as e:
        print(f"Email error: {str(e)}")
        return {"status": "error", "message": f"Email error: {str(e)}"}

async def send_result_email(notice: str, last4: str, email: str, result: dict):
    """Send the clean hands result via email"""
    
    status = result.get("status", "unknown")
    message = result.get("message", "No additional information")
    
    subject = f"DC Clean Hands Certificate Check - Notice {notice}"
    
    html_body = f"""
    <html>
    <body>
        <h2>DC Clean Hands Certificate Check Results</h2>
        <p><strong>Notice Number:</strong> {notice}</p>
        <p><strong>Last 4 Digits:</strong> {last4}</p>
        <p><strong>Status:</strong> {status.upper()}</p>
        <p><strong>Details:</strong> {message}</p>
        
        {"<p style='color: green;'><strong>✓ COMPLIANT</strong> - Your certificate is valid.</p>" if status == "compliant" else ""}
        {"<p style='color: red;'><strong>✗ NON-COMPLIANT</strong> - Issues found with certificate.</p>" if status == "noncompliant" else ""}
        {"<p style='color: orange;'><strong>? UNKNOWN</strong> - Unable to determine status.</p>" if status == "unknown" else ""}
        
        <hr>
        <p><small>This is an automated message from the DC Clean Hands Checker service.</small></p>
        <p><small>🤖 Generated with Claude Code</small></p>
    </body>
    </html>
    """
    
    text_body = f"""
    DC Clean Hands Certificate Check Results
    
    Notice Number: {notice}
    Last 4 Digits: {last4}
    Status: {status.upper()}
    Details: {message}
    
    This is an automated message from the DC Clean Hands Checker service.
    🤖 Generated with Claude Code
    """
    
    return await send_email_via_cloudmailin(
        to_email=email,
        subject=subject, 
        html_body=html_body,
        text_body=text_body,
        pdf_path=result.get("pdf_path")
    )

@app.get("/_health")
async def health():
    return {"ok": True, "mode": "test"}

@app.post("/clean-hands")
async def test_clean_hands(req: CleanHandsRequest, bg: BackgroundTasks):
    """Test endpoint with mock browser automation"""
    
    print(f"TEST MODE: Processing notice {req.notice}, last4 {req.last4}, email {req.email}")
    
    # Generate session ID
    session_id = f"test-{req.notice}-{req.last4}"
    
    try:
        # Mock browser automation 
        result = await mock_clean_hands_workflow(req.notice, req.last4, session_id)
        
        # Send email in background
        bg.add_task(send_result_email, req.notice, req.last4, req.email, result)
        
        return {
            "status": result["status"],
            "notice": req.notice,
            "last4": req.last4, 
            "email_enqueued": True,
            "message": "Test mode - email will be sent with mock results",
            "session_id": session_id
        }
        
    except Exception as e:
        print(f"Error in test workflow: {str(e)}")
        return {
            "status": "error",
            "notice": req.notice,
            "last4": req.last4,
            "email_enqueued": False,
            "message": f"Test error: {str(e)}"
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("testapi:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)