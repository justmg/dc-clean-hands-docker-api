# Power Automate API Deployment Guide

## 📁 Files Created

✅ **`power_automate_api.py`** - Main Power Automate-compatible API  
✅ **`newdcagent.py`** - Proven DC Clean Hands workflow (working)  
✅ **`requirements.txt`** - All required dependencies

## 🚀 Deployment Options

### Option 1: Local Development Server

```bash
# Activate virtual environment
& myenv/Scripts/Activate.ps1

# Install dependencies (if needed)
pip install -r requirements.txt

# Start the API server
python power_automate_api.py
```

### Option 2: Production Deployment (Render/Railway/etc.)

1. **Create deployment repository**
2. **Add these files:**

   - `power_automate_api.py`
   - `newdcagent.py`
   - `requirements.txt`
   - `Procfile` (web: uvicorn power_automate_api:app --host 0.0.0.0 --port $PORT)

3. **Set environment variables:**
   - `PORT=8000` (or platform default)
   - `LOG_LEVEL=INFO`

## 🎯 API Endpoints

### Main Endpoint for Power Automate

**POST** `/check-clean-hands`

**Request Body:**

```json
{
  "notice": "L0012322733",
  "last4": "3283",
  "email": "user@example.com"
}
```

**Response:**

```json
{
  "status": "noncompliant",
  "notice": "L0012322733",
  "last4": "3283",
  "email": "user@example.com",
  "message": "Detected compliance status from page.",
  "pdf_path": "/path/to/file.pdf",
  "pdf_base64": "JVBERi0xLjQKJdPr6eEK...",
  "pdf_available": true,
  "urls_visited": ["https://mytax.dc.gov/_/"],
  "processing_time_seconds": 15.32,
  "success": true
}
```

### Other Endpoints

- **GET** `/` - API documentation
- **GET** `/health` - Health check
- **POST** `/test-workflow` - Test with hardcoded values
- **GET** `/download-pdf/{filename}` - Direct PDF download
- **GET** `/list-artifacts` - List available PDFs

## 💼 Power Automate Integration

### 1. Create HTTP Action

**Method:** `POST`  
**URI:** `https://your-domain.com/check-clean-hands`  
**Headers:**

```json
{
  "Content-Type": "application/json"
}
```

**Body:**

```json
{
  "notice": "@{variables('NoticeNumber')}",
  "last4": "@{variables('Last4Digits')}",
  "email": "@{variables('EmailAddress')}"
}
```

### 2. Parse JSON Response

Add a "Parse JSON" action with this schema:

```json
{
  "type": "object",
  "properties": {
    "status": { "type": "string" },
    "notice": { "type": "string" },
    "last4": { "type": "string" },
    "email": { "type": "string" },
    "message": { "type": "string" },
    "pdf_path": { "type": ["string", "null"] },
    "pdf_base64": { "type": ["string", "null"] },
    "pdf_available": { "type": "boolean" },
    "urls_visited": { "type": "array" },
    "processing_time_seconds": { "type": "number" },
    "success": { "type": "boolean" }
  }
}
```

### 3. Use the Results

**Check Compliance Status:**

```
@{body('Parse_JSON')?['status']}
```

**Get PDF Content:**

```
@{body('Parse_JSON')?['pdf_base64']}
```

**Create PDF File:**

1. Add "Create file" action (SharePoint/OneDrive)
2. File name: `Clean_Hands_@{body('Parse_JSON')?['notice']}.pdf`
3. File content: `@{base64ToBinary(body('Parse_JSON')?['pdf_base64'])}`

## 🔄 Example Power Automate Flow

```
1. Manual Trigger / Form Input
   ↓
2. Set Variables (Notice, Last4, Email)
   ↓
3. HTTP - POST to /check-clean-hands
   ↓
4. Parse JSON Response
   ↓
5. Condition: Is PDF Available?
   ├─ Yes: Save PDF to SharePoint/OneDrive
   └─ No: Continue without PDF
   ↓
6. Send Email with Results
   ├─ Subject: "Clean Hands Check - @{body('Parse_JSON')?['status']}"
   ├─ Body: Include compliance status and message
   └─ Attach PDF (if available)
```

## 🛠️ Testing

### Local Testing

```bash
# Test the health endpoint
curl http://localhost:8000/health

# Test with sample data
curl -X POST http://localhost:8000/test-workflow

# Test with custom data
curl -X POST http://localhost:8000/check-clean-hands \
  -H "Content-Type: application/json" \
  -d '{"notice":"L0012322733","last4":"3283","email":"test@example.com"}'
```

### Power Automate Testing

1. Create a simple test flow
2. Use the test endpoint first: `/test-workflow`
3. Gradually build up to the full integration

## 📊 Response Status Meanings

- **`compliant`** - Certificate is valid and compliant
- **`noncompliant`** - Certificate has issues (most common case)
- **`unknown`** - Could not determine status (rare)
- **`error`** - Processing failed (check message for details)

## ⚠️ Important Notes

1. **PDF Availability**: PDFs are only available for successfully processed requests
2. **Rate Limiting**: The DC MyTax site may have rate limits - add delays between requests
3. **Error Handling**: Always check the `success` field and handle errors gracefully
4. **PDF Size**: Base64 PDFs can be large - consider using the direct download endpoint for large files
5. **Security**: Deploy with HTTPS in production
6. **Timeouts**: Allow up to 5 minutes for processing complex requests

## 🔧 Troubleshooting

### Common Issues:

1. **"Could not find validate link"** - Website structure changed or rate limiting
2. **"PDF not available"** - Some certificates don't generate PDFs immediately
3. **"Processing timeout"** - Increase timeout limits or retry logic
4. **"Import errors"** - Ensure all files are in the same directory

### Solutions:

- Add retry logic in Power Automate
- Use conditional logic for PDF handling
- Implement error notifications
- Monitor processing times and adjust timeouts

## 🎉 Success Indicators

✅ API returns status 200  
✅ `success: true` in response  
✅ Valid compliance status (`compliant`/`noncompliant`)  
✅ PDF downloaded when available  
✅ Processing completes within expected timeframe

The API is production-ready and proven to work with the DC MyTax system! 🚀
