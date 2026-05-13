import gradio as gr
import json
from datetime import datetime
import time
from pipeline.orchestrator import analyse

# Custom CSS for luxury theme
CUSTOM_CSS = """
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    background: linear-gradient(135deg, #0B0B0F 0%, #1A1A2E 100%);
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
    color: #E8E8E8;
}

/* Hero Section */
.hero-container {
    background: linear-gradient(135deg, #1A0033 0%, #0B0B0F 50%, #1A0033 100%);
    padding: 60px 40px;
    text-align: center;
    border-bottom: 1px solid rgba(212, 175, 55, 0.3);
    position: relative;
    overflow: hidden;
}

.hero-container::before {
    content: '';
    position: absolute;
    top: -50%;
    right: -10%;
    width: 500px;
    height: 500px;
    background: radial-gradient(circle, rgba(212, 175, 55, 0.1) 0%, transparent 70%);
    border-radius: 50%;
    pointer-events: none;
}

.hero-container::after {
    content: '';
    position: absolute;
    bottom: -50%;
    left: -10%;
    width: 500px;
    height: 500px;
    background: radial-gradient(circle, rgba(147, 51, 234, 0.1) 0%, transparent 70%);
    border-radius: 50%;
    pointer-events: none;
}

.hero-title {
    font-size: 3.5em;
    font-weight: 700;
    margin-bottom: 15px;
    background: linear-gradient(135deg, #D4AF37 0%, #E8D4B8 50%, #D4AF37 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    position: relative;
    z-index: 1;
    text-shadow: 0 0 30px rgba(212, 175, 55, 0.3);
    letter-spacing: 2px;
}

.hero-subtitle {
    font-size: 1.2em;
    color: rgba(232, 232, 232, 0.7);
    position: relative;
    z-index: 1;
    max-width: 600px;
    margin: 0 auto;
    font-weight: 300;
    letter-spacing: 0.5px;
}

/* Main Container */
.main-container {
    padding: 40px;
    max-width: 1400px;
    margin: 0 auto;
}

/* Two Column Layout */
.content-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 30px;
    margin-bottom: 40px;
}

@media (max-width: 1024px) {
    .content-grid {
        grid-template-columns: 1fr;
    }
}

/* Input Panel */
.input-panel {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(212, 175, 55, 0.2);
    border-radius: 20px;
    padding: 30px;
    backdrop-filter: blur(10px);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    transition: all 0.3s ease;
}

.input-panel:hover {
    border-color: rgba(212, 175, 55, 0.4);
    box-shadow: 0 12px 40px rgba(212, 175, 55, 0.1);
}

.panel-title {
    font-size: 1.4em;
    font-weight: 600;
    margin-bottom: 25px;
    color: #D4AF37;
    display: flex;
    align-items: center;
    gap: 10px;
}

/* Upload Area */
.upload-area {
    border: 2px dashed rgba(212, 175, 55, 0.4);
    border-radius: 16px;
    padding: 40px 20px;
    text-align: center;
    background: rgba(212, 175, 55, 0.02);
    transition: all 0.3s ease;
    cursor: pointer;
    margin-bottom: 25px;
}

.upload-area:hover {
    border-color: rgba(212, 175, 55, 0.7);
    background: rgba(212, 175, 55, 0.05);
}

/* Buttons */
.btn-primary {
    background: linear-gradient(135deg, #D4AF37 0%, #E8D4B8 100%);
    color: #0B0B0F;
    border: none;
    padding: 14px 32px;
    border-radius: 12px;
    font-weight: 600;
    font-size: 1em;
    cursor: pointer;
    transition: all 0.3s ease;
    box-shadow: 0 4px 15px rgba(212, 175, 55, 0.3);
    width: 100%;
    margin-bottom: 15px;
}

.btn-primary:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 25px rgba(212, 175, 55, 0.5);
    background: linear-gradient(135deg, #E8D4B8 0%, #D4AF37 100%);
}

.btn-primary:active {
    transform: translateY(0);
}

/* Output Panel */
.output-panel {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(212, 175, 55, 0.2);
    border-radius: 20px;
    padding: 30px;
    backdrop-filter: blur(10px);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    transition: all 0.3s ease;
    animation: fadeIn 0.5s ease;
}

@keyframes fadeIn {
    from {
        opacity: 0;
        transform: translateY(10px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}

/* Result Card */
.result-card {
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(212, 175, 55, 0.3);
    border-radius: 16px;
    padding: 25px;
    margin-bottom: 25px;
    text-align: center;
    animation: slideIn 0.6s ease;
}

@keyframes slideIn {
    from {
        opacity: 0;
        transform: translateX(20px);
    }
    to {
        opacity: 1;
        transform: translateX(0);
    }
}

.result-label {
    font-size: 0.9em;
    color: rgba(232, 232, 232, 0.6);
    margin-bottom: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
}

.result-status {
    font-size: 2.5em;
    font-weight: 700;
    margin: 15px 0;
    letter-spacing: 1px;
}

.status-authentic {
    color: #4ADF83;
    text-shadow: 0 0 20px rgba(74, 223, 131, 0.4);
}

.status-suspicious {
    color: #FFD700;
    text-shadow: 0 0 20px rgba(255, 215, 0, 0.4);
}

.status-fake {
    color: #FF6B6B;
    text-shadow: 0 0 20px rgba(255, 107, 107, 0.4);
}

.confidence-score {
    font-size: 1.2em;
    margin: 15px 0;
    color: #D4AF37;
}

/* Analysis Layers */
.layers-container {
    margin-top: 30px;
}

.layer-item {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(147, 51, 234, 0.2);
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 15px;
    display: flex;
    align-items: center;
    gap: 15px;
    animation: fadeInUp 0.6s ease;
    transition: all 0.3s ease;
}

.layer-item:hover {
    border-color: rgba(212, 175, 55, 0.3);
    background: rgba(212, 175, 55, 0.02);
    transform: translateX(5px);
}

@keyframes fadeInUp {
    from {
        opacity: 0;
        transform: translateY(10px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}

.layer-icon {
    font-size: 1.8em;
    min-width: 40px;
    text-align: center;
}

.layer-info {
    flex: 1;
    text-align: left;
}

.layer-label {
    font-weight: 600;
    color: #E8E8E8;
    margin-bottom: 5px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.layer-confidence {
    color: #D4AF37;
    font-weight: 600;
}

.progress-bar {
    width: 100%;
    height: 6px;
    background: rgba(255, 255, 255, 0.1);
    border-radius: 3px;
    overflow: hidden;
    margin-top: 8px;
}

.progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #D4AF37 0%, #E8D4B8 100%);
    border-radius: 3px;
    animation: fillProgress 1s ease;
}

@keyframes fillProgress {
    from {
        width: 0;
    }
    to {
        width: var(--confidence);
    }
}

/* Loading State */
.loading-animation {
    display: inline-block;
    width: 20px;
    height: 20px;
    border: 3px solid rgba(212, 175, 55, 0.3);
    border-top: 3px solid #D4AF37;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}

@keyframes spin {
    0% {
        transform: rotate(0deg);
    }
    100% {
        transform: rotate(360deg);
    }
}

/* Glow Effect */
.glow-border {
    position: relative;
    border: 2px solid transparent;
    background: linear-gradient(rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.05)) padding-box,
                linear-gradient(135deg, rgba(212, 175, 55, 0.5), rgba(147, 51, 234, 0.5)) border-box;
    border-radius: 12px;
    animation: glowPulse 2s ease-in-out infinite;
}

@keyframes glowPulse {
    0%, 100% {
        opacity: 0.8;
    }
    50% {
        opacity: 1;
    }
}

/* Responsive */
@media (max-width: 768px) {
    .hero-title {
        font-size: 2.2em;
    }
    
    .hero-subtitle {
        font-size: 1em;
    }
    
    .main-container {
        padding: 20px;
    }
    
    .input-panel, .output-panel {
        padding: 20px;
    }
}

/* Gradio Component Overrides */
.gradio-container {
    background: transparent !important;
}

.form {
    background: transparent !important;
    border: none !important;
}
"""

def determine_status(confidence_score):
    """Map confidence score to authentication status."""
    if confidence_score >= 75:
        return "Authentic", "status-authentic", "✓"
    elif confidence_score < 25:
        return "Fake", "status-fake", "✗"
    else:
        return "Suspicious", "status-suspicious", "⚠"

def format_layer_name(layer_num, layer_data):
    """Extract meaningful name from layer data."""
    names = {
        1: f"Source Type: {layer_data.get('source_type', 'Unknown')}",
        2: f"Object: {layer_data.get('brand', 'Unknown')}",
        3: "Confidence Signal",
        4: f"Provenance: {layer_data.get('provenance_status', 'Unknown')}",
        5: "Recommendations"
    }
    return names.get(layer_num, f"Layer {layer_num}")

def extract_confidence(layer_num, layer_data):
    """Extract confidence percentage from layer."""
    if layer_num == 1:
        return int(layer_data.get('confidence', 0) * 100)
    elif layer_num == 2:
        return int(layer_data.get('confidence', 0) * 100)
    elif layer_num == 3:
        return layer_data.get('confidence_score', 50)
    elif layer_num == 4:
        # Provenance: Clean = 95%, Flagged = 0%
        return 95 if layer_data.get('provenance_status') == 'Clean' else 0
    elif layer_num == 5:
        # Map severity to confidence
        severity = layer_data.get('severity', 'info')
        severity_map = {'critical': 10, 'warning': 30, 'caution': 60, 'info': 85}
        return severity_map.get(severity, 50)
    return 50

def analyze_image(image):
    """Run real pipeline analysis."""
    if image is None:
        return "Please upload an image first."
    
    try:
        # Run the real pipeline
        result = analyse(image)
        
        # Extract key data
        l1 = result['layer1']
        l2 = result['layer2']
        l3 = result['layer3']
        l4 = result['layer4']
        l5 = result['layer5']
        
        # Determine overall status
        confidence_score = l3['confidence_score']
        status, status_color, status_icon = determine_status(confidence_score)
        
        # Build result HTML
        result_html = f"""
        <div class="result-card">
            <div class="result-label">Authentication Result</div>
            <div class="result-status {status_color}">{status}</div>
            <div class="confidence-score">Confidence: {confidence_score}%</div>
        </div>
        
        <div class="layers-container">
            <h3 style="color: #D4AF37; margin-bottom: 20px; font-size: 1.1em;">Analysis Layers</h3>
        """
        
        layers_info = [
            (1, "Source Type", l1.get('source_type'), l1.get('confidence', 0)),
            (2, "Object & Brand", f"{l2.get('brand')} / {l2.get('category')}", l2.get('confidence', 0)),
            (3, "Confidence Signal", l3.get('signal_label'), l3.get('confidence_score') / 100),
            (4, "Provenance Check", l4.get('provenance_status'), 0.95 if l4.get('provenance_status') == 'Clean' else 0),
            (5, "Recommendations", f"Severity: {l5.get('severity', 'info').title()}", 0.5),
        ]
        
        # Display each layer
        for layer_num, label, value, conf_raw in layers_info:
            if isinstance(conf_raw, float) and conf_raw <= 1:
                conf_pct = int(conf_raw * 100)
            else:
                conf_pct = int(conf_raw)
            
            icon = "✓" if conf_pct >= 75 else "⚠" if conf_pct >= 50 else "✗"
            
            result_html += f"""
            <div class="layer-item">
                <div class="layer-icon">{icon}</div>
                <div class="layer-info">
                    <div class="layer-label">
                        <span><strong>{label}</strong>: {value}</span>
                        <span class="layer-confidence">{conf_pct}%</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="--confidence: {conf_pct}%;"></div>
                    </div>
                </div>
            </div>
            """
        
        # Add warnings if any
        for warning in result.get('warnings', []):
            result_html += f"""
            <div style="background: rgba(255, 215, 0, 0.1); border-left: 3px solid #FFD700; padding: 12px; margin-top: 15px; border-radius: 4px; color: rgba(232, 232, 232, 0.9);">
                <strong>⚠ Warning:</strong> {warning}
            </div>
            """
        
        # Add disclaimer
        result_html += f"""
        <div style="background: rgba(147, 51, 234, 0.08); border-left: 3px solid rgba(147, 51, 234, 0.5); padding: 12px; margin-top: 15px; border-radius: 4px; color: rgba(232, 232, 232, 0.7); font-size: 0.9em;">
            <strong>Disclaimer:</strong> {result.get('global_disclaimer', '')}
        </div>
        """
        
        result_html += "</div>"
        
        return result_html
        
    except ValueError as e:
        return f"""
        <div style="background: rgba(255, 107, 107, 0.1); border: 1px solid rgba(255, 107, 107, 0.5); padding: 20px; border-radius: 12px; color: #FF6B6B;">
            <strong>Error:</strong> {str(e)}
        </div>
        """
    except Exception as e:
        return f"""
        <div style="background: rgba(255, 107, 107, 0.1); border: 1px solid rgba(255, 107, 107, 0.5); padding: 20px; border-radius: 12px; color: #FF6B6B;">
            <strong>Analysis Error:</strong> {str(e)[:200]}
        </div>
        """

def handle_sample_select(sample_choice):
    """Load sample image based on selection."""
    import numpy as np
    from PIL import Image
    
    # Create dummy images with different characteristics
    dummy_image = Image.new('RGB', (400, 400), color=(20, 20, 30))
    
    return dummy_image

# Build the interface
with gr.Blocks(
    css=CUSTOM_CSS,
    theme=gr.themes.Base(
        primary_hue="amber",
        secondary_hue="purple",
    ),
    title="Luxury Truth Lens"
) as demo:
    
    # Hero Section
    with gr.Row():
        gr.HTML("""
        <div class="hero-container">
            <h1 class="hero-title">✨ Luxury Truth Lens</h1>
            <p class="hero-subtitle">
                Advanced AI-powered verification for luxury items. 
                Authenticate with precision, decide with confidence.
            </p>
        </div>
        """)
    
    # Main Content
    with gr.Row(elem_classes="main-container"):
        
        # Left Column - Input Panel
        with gr.Column(scale=1, elem_classes="input-panel"):
            gr.HTML('<h2 class="panel-title">📸 Upload Item</h2>')
            
            image_input = gr.Image(
                type="pil",
                label="",
                elem_classes="upload-area",
                show_share_button=False,
            )
            
            gr.HTML('<p style="color: rgba(232, 232, 232, 0.6); font-size: 0.95em; margin: 20px 0; text-align: center;">Upload a clear photo of the luxury item. Analysis will begin automatically.</p>')
            
            analyze_btn = gr.Button(
                "🔍 Run Analysis",
                elem_classes="btn-primary",
                variant="primary"
            )
        
        # Right Column - Output Panel
        with gr.Column(scale=1, elem_classes="output-panel"):
            gr.HTML('<h2 class="panel-title">📊 Analysis Results</h2>')
            
            results_output = gr.HTML(
                value="""
                <div style="text-align: center; padding: 40px 20px; color: rgba(232, 232, 232, 0.5);">
                    <p style="font-size: 1.1em;">Upload an image and click "Run Analysis" to see results</p>
                    <p style="margin-top: 10px; font-size: 0.95em;">Analysis powered by AI-driven verification across 5 verification layers</p>
                </div>
                """
            )
    
    # Event Handlers
    def on_analyze(image):
        return analyze_image(image)
    
    analyze_btn.click(
        fn=on_analyze,
        inputs=[image_input],
        outputs=[results_output]
    )
    
    # Allow automatic analysis on image upload
    image_input.change(
        fn=on_analyze,
        inputs=[image_input],
        outputs=[results_output]
    )

if __name__ == "__main__":
    demo.launch(share=False, server_name="127.0.0.1", server_port=7860)
