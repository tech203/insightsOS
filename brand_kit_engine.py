def generate_brand_kit(
    business_name="",
    industry="",
    location="",
    services="",
    business_context=None,
):
    business_context = business_context or {}

    combined = f"{industry} {services}".lower()

    if any(word in combined for word in ["ice cream", "dessert", "confectionery", "food", "cafe", "restaurant", "bakery", "f&b"]):
        return {
            "brand_name": business_name,
            "industry_theme": "food_and_beverage",
            "personality": ["playful", "warm", "nostalgic", "friendly"],
            "tone_of_voice": "friendly, appetising, simple, inviting",
            "visual_style": "modern dessert brand",
            "primary_color": "#f97316",
            "secondary_color": "#fff7ed",
            "accent_color": "#fed7aa",
            "text_color": "#0f172a",
            "background_color": "#ffffff",
            "font_style": "rounded modern",
            "imagery_style": "bright product photography, desserts, lifestyle shots",
            "primary_cta": "Shop Now",
            "secondary_cta": "View Products",
            "hero_direction": "Warm ecommerce hero with product showcase",
        }

    if any(word in combined for word in ["dental", "clinic", "medical", "health", "aesthetic"]):
        return {
            "brand_name": business_name,
            "industry_theme": "clinic",
            "personality": ["professional", "calm", "trustworthy", "reassuring"],
            "tone_of_voice": "clear, professional, reassuring",
            "visual_style": "clean healthcare brand",
            "primary_color": "#2563eb",
            "secondary_color": "#eff6ff",
            "accent_color": "#bfdbfe",
            "text_color": "#0f172a",
            "background_color": "#ffffff",
            "font_style": "clean modern",
            "imagery_style": "clinic environment, smiling patients, clean spaces",
            "primary_cta": "Book Appointment",
            "secondary_cta": "View Services",
            "hero_direction": "Trust-focused healthcare hero",
        }

    if any(word in combined for word in ["tuition", "education", "enrichment", "school", "learning"]):
        return {
            "brand_name": business_name,
            "industry_theme": "education",
            "personality": ["supportive", "clear", "encouraging", "parent-friendly"],
            "tone_of_voice": "helpful, encouraging, simple",
            "visual_style": "friendly education brand",
            "primary_color": "#7c3aed",
            "secondary_color": "#f5f3ff",
            "accent_color": "#ddd6fe",
            "text_color": "#0f172a",
            "background_color": "#ffffff",
            "font_style": "friendly modern",
            "imagery_style": "students, learning, classroom, progress",
            "primary_cta": "Enquire Now",
            "secondary_cta": "View Programmes",
            "hero_direction": "Parent-friendly education hero",
        }

    return {
        "brand_name": business_name,
        "industry_theme": "general",
        "personality": ["clear", "professional", "trustworthy"],
        "tone_of_voice": "clear, professional, simple",
        "visual_style": "clean business website",
        "primary_color": "#4f46e5",
        "secondary_color": "#eef2ff",
        "accent_color": "#c7d2fe",
        "text_color": "#0f172a",
        "background_color": "#ffffff",
        "font_style": "modern professional",
        "imagery_style": "business, people, service experience",
        "primary_cta": "Enquire Now",
        "secondary_cta": "View Services",
        "hero_direction": "Trust-focused business hero",
    }