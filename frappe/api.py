# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

import base64
import binascii
import json
from urllib.parse import urlencode, urlparse

import frappe
import frappe.client
import frappe.handler
from frappe import _
from frappe.utils.data import sbool
from frappe.utils.response import build_response


def handle():
	"""
	Handler for `/api` methods

	### Examples:

	`/api/method/{methodname}` will call a whitelisted method

	`/api/resource/{doctype}` will query a table
	        examples:
	        - `?fields=["name", "owner"]`
	        - `?filters=[["Task", "name", "like", "%005"]]`
	        - `?limit_start=0`
	        - `?limit_page_length=20`

	`/api/resource/{doctype}/{name}` will point to a resource
	        `GET` will return doclist
	        `POST` will insert
	        `PUT` will update
	        `DELETE` will delete

	`/api/resource/{doctype}/{name}?run_method={method}` will run a whitelisted controller method
	"""

	validate_auth()

	parts = frappe.request.path[1:].split("/",3)
	call = doctype = name = None

	if len(parts) > 1:
		call = parts[1]

	if len(parts) > 2:
		doctype = parts[2]

	if len(parts) > 3:
		name = parts[3]

	if call == "method":
		frappe.local.form_dict.cmd = doctype
		return frappe.handler.handle()

	elif call == "resource":
		if "run_method" in frappe.local.form_dict:
			method = frappe.local.form_dict.pop("run_method")
			doc = frappe.get_doc(doctype, name)
			doc.is_whitelisted(method)

			if frappe.local.request.method == "GET":
				if not doc.has_permission("read"):
					frappe.throw(_("Not permitted"), frappe.PermissionError)
				frappe.local.response.update({"data": doc.run_method(method, **frappe.local.form_dict)})

			if frappe.local.request.method == "POST":
				if not doc.has_permission("write"):
					frappe.throw(_("Not permitted"), frappe.PermissionError)

				frappe.local.response.update({"data": doc.run_method(method, **frappe.local.form_dict)})
				frappe.db.commit()

		else:
			if name:
				if frappe.local.request.method == "GET":
					doc = frappe.get_doc(doctype, name)
					if not doc.has_permission("read"):
						raise frappe.PermissionError
					frappe.local.response.update({"data": doc})

				if frappe.local.request.method == "PUT":
					data = get_request_form_data()

					doc = frappe.get_doc(doctype, name, for_update=True)

					if "flags" in data:
						del data["flags"]

					# Not checking permissions here because it's checked in doc.save
					doc.update(data)

					frappe.local.response.update({"data": doc.save().as_dict()})

					if doc.parenttype and doc.parent:
						frappe.get_doc(doc.parenttype, doc.parent).save()

					frappe.db.commit()

				if frappe.local.request.method == "DELETE":
					# Not checking permissions here because it's checked in delete_doc
					frappe.delete_doc(doctype, name, ignore_missing=False)
					frappe.local.response.http_status_code = 202
					frappe.local.response.message = "ok"
					frappe.db.commit()

			elif doctype:
				if frappe.local.request.method == "GET":
					# set fields for frappe.get_list
					if frappe.local.form_dict.get("fields"):
						frappe.local.form_dict["fields"] = json.loads(frappe.local.form_dict["fields"])

					# set limit of records for frappe.get_list
					frappe.local.form_dict.setdefault(
						"limit_page_length",
						frappe.local.form_dict.limit or frappe.local.form_dict.limit_page_length or 20,
					)

					# convert strings to native types - only as_dict and debug accept bool
					for param in ["as_dict", "debug"]:
						param_val = frappe.local.form_dict.get(param)
						if param_val is not None:
							frappe.local.form_dict[param] = sbool(param_val)

					# evaluate frappe.get_list
					data = frappe.call(frappe.client.get_list, doctype, **frappe.local.form_dict)

					# set frappe.get_list result to response
					frappe.local.response.update({"data": data})

				if frappe.local.request.method == "POST":
					# fetch data from from dict
					data = get_request_form_data()
					data.update({"doctype": doctype})

					# insert document from request data
					doc = frappe.get_doc(data).insert()

					# set response data
					frappe.local.response.update({"data": doc.as_dict()})

					# commit for POST requests
					frappe.db.commit()
			else:
				raise frappe.DoesNotExistError

	else:
		raise frappe.DoesNotExistError

	return build_response("json")


def get_request_form_data():
	if frappe.local.form_dict.data is None:
		data = frappe.safe_decode(frappe.local.request.get_data())
	else:
		data = frappe.local.form_dict.data

	return frappe.parse_json(data)


def validate_auth():
	"""
	Authenticate and sets user for the request.
	"""

	if frappe.get_request_header("Authorization") is None:
		return

	VALID_AUTH_PREFIX_TYPES = ['basic', 'bearer', 'token']
	VALID_AUTH_PREFIX_STRING = ", ".join(VALID_AUTH_PREFIX_TYPES).title()

	authorization_header = frappe.get_request_header("Authorization", str()).split(" ")
	authorization_type = authorization_header[0].lower()

	if len(authorization_header) == 1:
		frappe.throw(_('Invalid Authorization headers, add a token with a prefix from one of the following: {0}.'.format(VALID_AUTH_PREFIX_STRING)), frappe.InvalidAuthorizationHeader)

	if authorization_type == "bearer":
		validate_oauth(authorization_header)
	elif authorization_type in VALID_AUTH_PREFIX_TYPES:
		validate_auth_via_api_keys(authorization_header)
	elif authorization_type == "next":
		validate_jwt(authorization_header)
	else:
		frappe.throw(_('Invalid Authorization Type {0}, must be one of {1}.'.format(authorization_type, VALID_AUTH_PREFIX_STRING)), frappe.InvalidAuthorizationPrefix)


def validate_oauth(authorization_header):
	"""
	Authenticate request using OAuth and set session user

	Args:
	        authorization_header (list of str): The 'Authorization' header containing the prefix and token
	"""

	print("validate o auth")
	print(authorization_header, "o auth")

	from frappe.oauth import get_url_delimiter
	from frappe.integrations.oauth2 import get_oauth_server

	form_dict = frappe.local.form_dict
	token = authorization_header[1]
	req = frappe.request
	parsed_url = urlparse(req.url)
	access_token = {"access_token": token}
	uri = (
		parsed_url.scheme + "://" + parsed_url.netloc + parsed_url.path + "?" + urlencode(access_token)
	)
	http_method = req.method
	body = req.get_data()
	headers = req.headers

	try:
		required_scopes = frappe.db.get_value("OAuth Bearer Token", token, "scopes").split(get_url_delimiter())
	except AttributeError:
		frappe.throw(_("Invalid Bearer token, please provide a valid access token with prefix 'Bearer'."), frappe.InvalidAuthorizationToken)

	valid, oauthlib_request = get_oauth_server().verify_request(uri, http_method, body, headers, required_scopes)

	if valid:
		frappe.set_user(frappe.db.get_value("OAuth Bearer Token", token, "user"))
		frappe.local.form_dict = form_dict


def validate_auth_via_api_keys(authorization_header):
	"""
	Authenticate request using API keys and set session user

	Args:
	        authorization_header (list of str): The 'Authorization' header containing the prefix and token
	"""

	try:
		auth_type, auth_token = authorization_header
		if auth_type.lower() == 'basic':
			api_key, api_secret = frappe.safe_decode(base64.b64decode(auth_token)).split(":")
			validate_api_key_secret(api_key, api_secret)
		elif auth_type.lower() == 'token':
			api_key, api_secret = auth_token.split(":")
			validate_api_key_secret(api_key, api_secret)
	except binascii.Error:
		frappe.throw(
			_("Failed to decode token, please provide a valid base64-encoded token."),
			frappe.InvalidAuthorizationToken,
		)
	except (AttributeError, TypeError, ValueError):
		frappe.throw(_("Invalid token, please provide a valid token with prefix 'Basic' or 'Token'."), frappe.InvalidAuthorizationToken)



def validate_api_key_secret(api_key, api_secret, frappe_authorization_source=None):
	"""frappe_authorization_source to provide api key and secret for a doctype apart from User"""
	user = frappe.db.get_value(
		doctype="User",
		filters={"api_key": api_key},
		fieldname=['name']
	)
	form_dict = frappe.local.form_dict
	user_secret = frappe.utils.password.get_decrypted_password("User", user, fieldname='api_secret')
	if api_secret == user_secret:
		frappe.set_user(user)
		frappe.local.form_dict = form_dict


def validate_jwt(authorization_header):
	"""
		Next JWT authentication using api key and api secret
	"""
	import jwt
	from newmatik.next.helpers import get_jwt_config
	jwt_config = get_jwt_config()
	jwt_token = authorization_header[1]
	try:
		payload = jwt.decode(
			jwt_token, jwt_config['JWT_SECRET'], jwt_config['JWT_ALGORITHM']
		)
	except jwt.ExpiredSignatureError as e:
		raise ExpiredLoginException()

	frappe.local.user_id = payload['user_id']
	validate_api_key_secret(payload['key'], payload['secret'])

def validate_auth_via_hooks():
	for auth_hook in frappe.get_hooks("auth_hooks", []):
		frappe.get_attr(auth_hook)()

class ExpiredLoginException(Exception):
	http_status_code = 401
