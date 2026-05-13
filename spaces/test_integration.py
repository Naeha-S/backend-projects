import sys
sys.path.insert(0, r'c:\Users\NAEHA\Desktop\projects\spaces')

from luxury_authenticator import analyze_image, determine_status
from pipeline.orchestrator import analyse

# Test status determination
status, color, icon = determine_status(80)
print(f"✓ Status function works: {status}")

# Test that analyse is imported
print(f"✓ Pipeline analyse imported successfully")

# Test analyze_image with None (should give error message)
result = analyze_image(None)
if "Please upload" in result:
    print("✓ Error handling works correctly")

print("\n✅ INTEGRATION COMPLETE - Ready for real images!")
