"""
Webflow CMS Client Service

This module provides a reusable client for interacting with Webflow CMS collections.
It abstracts away API details and handles:
- Listing collections and their fields
- Creating and updating CMS items
- Publishing items (only if configured)
- Clear error handling without crashing

Usage:
    from services.webflow_client import WebflowCMSClient
    
    client = WebflowCMSClient()
    collections = client.list_collections()
    fields = client.list_collection_fields(collection_id)
    item_id = client.create_item(collection_id, field_data)
    client.update_item(collection_id, item_id, field_data)
    client.publish_items([item_id], collection_id)
"""

import os
from typing import Dict, List, Optional, Any
import logging

# Single source of truth: reuse the integration layer's HTTP request,
# API base, and exception types so both Webflow stacks share one error
# hierarchy (an `except WebflowAPIError` works no matter which layer
# raised it) and one place to maintain request behaviour.
from webflow_integration import (
    WEBFLOW_API_BASE,
    WebflowAPIError,
    WebflowConfigError,
    _webflow_request,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WEBFLOW_API_BASE",
    "WebflowAPIError",
    "WebflowConfigError",
    "WebflowCMSClient",
]


class WebflowCMSClient:
    """Safe, reusable client for Webflow CMS operations."""

    def __init__(self, api_token: Optional[str] = None, site_id: Optional[str] = None):
        """
        Initialize the Webflow CMS client.
        
        Args:
            api_token: Webflow API token (defaults to WEBFLOW_API_TOKEN env var)
            site_id: Webflow site ID (defaults to WEBFLOW_SITE_ID env var)
        """
        self.api_token = api_token or os.getenv("WEBFLOW_API_TOKEN")
        self.site_id = site_id or os.getenv("WEBFLOW_SITE_ID")
        self.publish_on_export = os.getenv("WEBFLOW_PUBLISH_ON_EXPORT", "false").strip().lower() in {"1", "true", "yes"}

        if not self.api_token or not self.site_id:
            raise WebflowConfigError(
                "WEBFLOW_API_TOKEN and WEBFLOW_SITE_ID environment variables must be set."
            )

    def _request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Make a request to the Webflow API via the shared integration-layer
        request function, so both Webflow stacks use one HTTP path, one
        error format, and one timeout policy.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            endpoint: API endpoint path
            data: Optional request body data

        Returns:
            Response JSON as dictionary

        Raises:
            WebflowAPIError: If the request fails
        """
        url = f"{WEBFLOW_API_BASE}{endpoint}"
        return _webflow_request({"token": self.api_token}, method, url, data)

    def test_connection(self) -> bool:
        """
        Test whether the Webflow connection is working.
        
        Returns:
            True if connection is successful
            
        Raises:
            WebflowAPIError: If connection fails
        """
        try:
            response = self._request("GET", f"/sites/{self.site_id}")
            return bool(response.get("id"))
        except WebflowAPIError:
            raise

    def list_collections(self) -> List[Dict[str, Any]]:
        """
        List all CMS collections for the site.
        
        Returns:
            List of collection dictionaries with id, singularName, displayName
        """
        try:
            response = self._request("GET", f"/sites/{self.site_id}/collections")
            return response.get("collections", [])
        except WebflowAPIError as e:
            logger.error(f"Failed to list collections: {e}")
            raise

    def list_collection_fields(self, collection_id: str) -> List[Dict[str, Any]]:
        """
        List all fields in a CMS collection.
        
        Args:
            collection_id: The collection ID
            
        Returns:
            List of field dictionaries with id, slug, displayName, type, required, etc.
        """
        try:
            response = self._request("GET", f"/collections/{collection_id}")
            return response.get("fields", [])
        except WebflowAPIError as e:
            logger.error(f"Failed to list fields for collection {collection_id}: {e}")
            raise

    def get_collection_details(self, collection_id: str) -> Dict[str, Any]:
        """
        Get full details for a collection.
        
        Args:
            collection_id: The collection ID
            
        Returns:
            Collection details including fields, displayName, singularName, slug
        """
        try:
            return self._request("GET", f"/collections/{collection_id}")
        except WebflowAPIError as e:
            logger.error(f"Failed to get collection details for {collection_id}: {e}")
            raise

    def create_item(self, collection_id: str, field_data: Dict[str, Any], is_draft: bool = True) -> str:
        """
        Create a new CMS item in a collection.
        
        Args:
            collection_id: The collection ID
            field_data: Dictionary of field_slug -> value pairs
            is_draft: Whether to create as draft (default True for safety)
            
        Returns:
            The created item's ID
            
        Raises:
            WebflowAPIError: If creation fails
        """
        try:
            payload = {
                "fieldData": field_data,
                "isDraft": is_draft,
                "isArchived": False,
            }
            response = self._request(
                "POST",
                f"/collections/{collection_id}/items",
                payload
            )
            item_id = response.get("id")
            if not item_id:
                raise WebflowAPIError("No item ID returned from create_item response")
            return item_id
        except WebflowAPIError as e:
            logger.error(f"Failed to create item in collection {collection_id}: {e}")
            raise

    def update_item(
        self,
        collection_id: str,
        item_id: str,
        field_data: Dict[str, Any],
        is_draft: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Update an existing CMS item.
        
        Args:
            collection_id: The collection ID
            item_id: The item ID to update
            field_data: Dictionary of field_slug -> value pairs to update
            is_draft: Optional - set draft status. If None, preserves current status.
            
        Returns:
            Updated item details
            
        Raises:
            WebflowAPIError: If update fails
        """
        try:
            payload = {
                "fieldData": field_data,
            }
            if is_draft is not None:
                payload["isDraft"] = is_draft

            response = self._request(
                "PATCH",
                f"/collections/{collection_id}/items/{item_id}",
                payload
            )
            return response
        except WebflowAPIError as e:
            logger.error(f"Failed to update item {item_id} in collection {collection_id}: {e}")
            raise

    def publish_items(self, item_ids: List[str], collection_id: str) -> Dict[str, Any]:
        """
        Publish one or more items in a collection.
        
        IMPORTANT: This will make items live on your Webflow site.
        Only call if WEBFLOW_PUBLISH_ON_EXPORT=true in .env
        
        Args:
            item_ids: List of item IDs to publish
            collection_id: The collection ID
            
        Returns:
            Publish result from API
            
        Raises:
            WebflowAPIError: If publishing fails
        """
        if not self.publish_on_export:
            logger.warning(f"Publishing disabled - WEBFLOW_PUBLISH_ON_EXPORT is not true")
            return {"message": "Publishing disabled - set WEBFLOW_PUBLISH_ON_EXPORT=true to enable"}

        try:
            payload = {
                "itemIds": item_ids,
            }
            response = self._request(
                "POST",
                f"/collections/{collection_id}/items/publish",
                payload
            )
            logger.info(f"Published {len(item_ids)} items in collection {collection_id}")
            return response
        except WebflowAPIError as e:
            logger.error(f"Failed to publish items in collection {collection_id}: {e}")
            raise

    def get_item(self, collection_id: str, item_id: str) -> Dict[str, Any]:
        """
        Get details for a specific CMS item.
        
        Args:
            collection_id: The collection ID
            item_id: The item ID
            
        Returns:
            Item details
            
        Raises:
            WebflowAPIError: If retrieval fails
        """
        try:
            return self._request("GET", f"/collections/{collection_id}/items/{item_id}")
        except WebflowAPIError as e:
            logger.error(f"Failed to get item {item_id}: {e}")
            raise

    def list_items(self, collection_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        List items in a collection.
        
        Args:
            collection_id: The collection ID
            limit: Maximum number of items to return (default 100)
            
        Returns:
            List of items
            
        Raises:
            WebflowAPIError: If retrieval fails
        """
        try:
            response = self._request(
                "GET",
                f"/collections/{collection_id}/items?limit={limit}"
            )
            return response.get("items", [])
        except WebflowAPIError as e:
            logger.error(f"Failed to list items in collection {collection_id}: {e}")
            raise

    def delete_item(self, collection_id: str, item_id: str) -> None:
        """
        Delete a CMS item.
        
        Args:
            collection_id: The collection ID
            item_id: The item ID to delete
            
        Raises:
            WebflowAPIError: If deletion fails
        """
        try:
            self._request("DELETE", f"/collections/{collection_id}/items/{item_id}")
            logger.info(f"Deleted item {item_id} from collection {collection_id}")
        except WebflowAPIError as e:
            logger.error(f"Failed to delete item {item_id}: {e}")
            raise
