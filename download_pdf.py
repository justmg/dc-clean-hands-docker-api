#!/usr/bin/env python3
"""
Standalone PDF downloader that takes a URL and downloads it to a specified path.
Can be called from the main script or run independently.
"""
import argparse
import sys
from pathlib import Path
import requests
from urllib.parse import urlparse


def download_pdf(url: str, output_path: str, timeout: int = 60) -> bool:
    """
    Download a PDF from the given URL to the specified path.
    
    Args:
        url: The PDF URL to download
        output_path: Path where to save the PDF
        timeout: Request timeout in seconds
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Create output directory if it doesn't exist
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Set headers to mimic browser request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/pdf,application/octet-stream,*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        # Add Referer for government sites
        if 'mytax.dc.gov' in url:
            headers['Referer'] = 'https://mytax.dc.gov/_/'
        
        print(f"Downloading PDF from: {url}")
        print(f"Saving to: {output_path}")
        
        # Download the PDF
        response = requests.get(url, headers=headers, timeout=timeout, stream=True)
        response.raise_for_status()
        
        # Check if response is actually a PDF
        content_type = response.headers.get('content-type', '').lower()
        if 'application/pdf' not in content_type and 'application/octet-stream' not in content_type:
            print(f"Warning: Content-Type is '{content_type}', not PDF")
        
        # Write to file
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Verify file was created and has content
        if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            print(f"✅ PDF downloaded successfully: {Path(output_path).stat().st_size} bytes")
            return True
        else:
            print("❌ PDF file was not created or is empty")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error downloading PDF: {e}")
        return False
    except Exception as e:
        print(f"❌ Error downloading PDF: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Download a PDF from a URL")
    parser.add_argument("url", help="PDF URL to download")
    parser.add_argument("output", help="Output file path")
    parser.add_argument("--timeout", type=int, default=60, help="Request timeout in seconds")
    
    args = parser.parse_args()
    
    success = download_pdf(args.url, args.output, args.timeout)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
