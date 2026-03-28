from flask import Flask, request, render_template_string
import config
from main import run_full_audit

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>

<title>AEO Audit Tool</title>

<script src="https://cdn.tailwindcss.com"></script>

</head>

<body class="bg-gray-100">

<div class="max-w-4xl mx-auto mt-10">

<h1 class="text-3xl font-bold mb-6">AI Visibility Audit</h1>

<div class="bg-white p-6 rounded shadow">

<form method="post">

<label class="font-semibold">Website</label>
<input class="border w-full p-2 mb-4" type="text" name="website" required>

<label class="font-semibold">Industry</label>
<input class="border w-full p-2 mb-4" type="text" name="industry">

<label class="font-semibold">Location</label>
<input class="border w-full p-2 mb-4" type="text" name="location">

<label class="font-semibold">Topic (optional)</label>
<input class="border w-full p-2 mb-4" type="text" name="topic">

<label class="font-semibold">Audit Type</label>

<select class="border w-full p-2 mb-4" name="audit_type">
<option value="quick">Quick Scan</option>
<option value="full">Full Audit</option>
</select>

<button class="bg-blue-600 text-white px-6 py-2 rounded">
Run Audit
</button>

</form>

</div>

{% if result %}

<div class="bg-white mt-6 p-6 rounded shadow">

<h2 class="text-xl font-bold mb-4">Audit Result</h2>

<pre class="whitespace-pre-wrap text-sm">{{ result }}</pre>

</div>

{% endif %}

</div>

</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    result = None

    if request.method == "POST":
        config.WEBSITE = request.form["website"]
        config.INDUSTRY = request.form["industry"]
        config.LOCATION = request.form["location"]
        config.AUDIT_TYPE = request.form["audit_type"]
        config.TOPIC = request.form.get("topic", config.INDUSTRY)

        result = run_full_audit()

    return render_template_string(HTML, result=result)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)