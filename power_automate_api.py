#!/usr/bin/env python3
"""
Power Automate-compatible API for DC Clean Hands automation
Uses the proven newdcagent.py workflow
"""
import asyncio
import os
import json
import base64
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field
from pathlib import Path
from dotenv import load_dotenv
import logging
from typing import Optional

# Import our working DC Clean Hands workflow
try:
    from newdcagent import run_workflow, WorkflowResult
    WORKFLOW_AVAILABLE = True
    print("✅ DC Clean Hands workflow available")
except Exception as e:
    WORKFLOW_AVAILABLE = False
    print(f"❌ DC Clean Hands workflow not available: {e}")

load_dotenv()

# Configuration
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("power_automate_api")

app = FastAPI(
    title="DC Clean Hands API for Power Automate", 
    version="1.0.0",
    description="Power Automate-compatible API for DC Clean Hands certificate checking"
)

print("🚀 Starting DC Clean Hands API for Power Automate...")
print(f"🗂️ Artifacts directory: {ARTIFACTS_DIR}")
print(f"🌐 Workflow available: {WORKFLOW_AVAILABLE}")

class CleanHandsRequest(BaseModel):
    notice: str = Field(..., min_length=5, max_length=64, description="Notice number (e.g. L0012322733)")
    last4: str = Field(..., pattern=r"^\d{4}$", description="Last 4 digits of taxpayer ID")
    email: EmailStr = Field(..., description="Email address for notifications")

class CleanHandsResponse(BaseModel):
    status: str = Field(..., description="Compliance status: compliant, noncompliant, or unknown")
    notice: str = Field(..., description="The notice number processed")
    last4: str = Field(..., description="Last 4 digits processed")
    email: str = Field(..., description="Email address")
    message: str = Field(..., description="Human-readable status message")
    pdf_path: Optional[str] = Field(None, description="Path to downloaded PDF file")
    pdf_base64: Optional[str] = Field(None, description="Base64-encoded PDF content for Power Automate")
    pdf_available: bool = Field(False, description="Whether PDF was successfully downloaded")
    urls_visited: list = Field(default_factory=list, description="URLs visited during the process")
    processing_time_seconds: float = Field(0.0, description="Total processing time")
    success: bool = Field(True, description="Whether the operation was successful")

async def process_clean_hands_request(notice: str, last4: str, email: str) -> CleanHandsResponse:
    """Process a Clean Hands request using our proven workflow"""
    
    if not WORKFLOW_AVAILABLE:
        raise HTTPException(status_code=500, detail="DC Clean Hands workflow not available")
    
    logger.info(f"🚀 Processing request - Notice: {notice}, Last4: {last4}, Email: {email}")
    
    import time
    start_time = time.time()
    
    try:
        # Run the proven workflow (headless, no screenshots, model_name ignored)
        result: WorkflowResult = await run_workflow(
            notice=notice,
            last4=last4,
            headless=True,  # Always headless for API
            screenshots=False,  # No screenshots for API
            model_name="api"  # Not used in deterministic workflow
        )
        
        processing_time = time.time() - start_time
        
        # Prepare PDF data for Power Automate
        pdf_base64 = None
        pdf_available = False
        
        if result.pdf_path and Path(result.pdf_path).exists():
            try:
                with open(result.pdf_path, "rb") as f:
                    pdf_content = f.read()
                    pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')
                    pdf_available = True
                logger.info(f"✅ PDF encoded for Power Automate: {len(pdf_content)} bytes")
            except Exception as e:
                logger.error(f"❌ Failed to encode PDF: {e}")
        
        # Create response
        response = CleanHandsResponse(
            status=result.status,
            notice=result.notice,
            last4=result.last4,
            email=email,
            message=result.message,
            pdf_path=result.pdf_path,
            pdf_base64=pdf_base64,
            pdf_available=pdf_available,
            urls_visited=result.urls,
            processing_time_seconds=round(processing_time, 2),
            success=True
        )
        
        logger.info(f"✅ Request completed successfully in {processing_time:.2f}s - Status: {result.status}")
        return response
        
    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(f"❌ Request failed after {processing_time:.2f}s: {str(e)}")
        
        # Return error response
        return CleanHandsResponse(
            status="error",
            notice=notice,
            last4=last4,
            email=email,
            message=f"Processing failed: {str(e)}",
            pdf_path=None,
            pdf_base64=None,
            pdf_available=False,
            urls_visited=[],
            processing_time_seconds=round(processing_time, 2),
            success=False
        )

@app.get("/")
async def root():
    """API root endpoint with documentation"""
    return {
        "service": "DC Clean Hands Certificate Checker",
        "version": "1.0.0",
        "platform": "Power Automate Compatible",
        "endpoints": {
            "health": "/health",
            "check_certificate": "/check-clean-hands",
            "download_pdf": "/download-pdf/{filename}"
        },
        "workflow_available": WORKFLOW_AVAILABLE,
        "usage": {
            "method": "POST",
            "endpoint": "/check-clean-hands",
            "body": {
                "notice": "L0012322733",
                "last4": "3283", 
                "email": "user@example.com"
            }
        }
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "workflow_available": WORKFLOW_AVAILABLE,
        "artifacts_dir": str(ARTIFACTS_DIR),
        "artifacts_exists": ARTIFACTS_DIR.exists()
    }

@app.post("/check-clean-hands", response_model=CleanHandsResponse)
async def check_clean_hands(request: CleanHandsRequest):
    """
    Main endpoint for Power Automate
    
    Processes a DC Clean Hands certificate check request and returns:
    - Compliance status (compliant/noncompliant/unknown)
    - PDF file as base64 (if available)
    - Processing details
    """
    
    logger.info(f"🎯 Power Automate request received - Notice: {request.notice}")
    
    response = await process_clean_hands_request(
        notice=request.notice,
        last4=request.last4,
        email=request.email
    )
    
    return response

@app.get("/download-pdf/{filename}")
async def download_pdf(filename: str):
    """Download PDF file directly (alternative to base64)"""
    
    pdf_path = ARTIFACTS_DIR / filename
    
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=filename
    )

@app.get("/list-artifacts")
async def list_artifacts():
    """List all available PDF artifacts"""
    
    pdf_files = list(ARTIFACTS_DIR.glob("*.pdf"))
    
    return {
        "artifacts_dir": str(ARTIFACTS_DIR),
        "pdf_files": [f.name for f in pdf_files],
        "total_files": len(pdf_files)
    }

# For testing/development
@app.post("/test-workflow")
async def test_workflow():
    """Test endpoint with hardcoded values"""
    
    test_request = CleanHandsRequest(
        notice="L0012322733",
        last4="3283",
        email="test@example.com"
    )
    
    return await check_clean_hands(test_request)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"🚀 Starting Power Automate API on port {port}")
    uvicorn.run("power_automate_api:app", host="0.0.0.0", port=port, reload=True)
