#include "vnxtptd.h"


///-------------------------------------------------------------------------------------
///C++的回调函数将数据保存到队列中
///-------------------------------------------------------------------------------------

void TdApi::OnDisconnected(uint64_t session_id, int reason)
{
	gil_scoped_acquire acquire;
	this->onDisconnected(session_id, reason);
};

void TdApi::OnError(XTPRI *error_info)
{
	gil_scoped_acquire acquire;
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onError(error);
};

void TdApi::OnOrderEvent(XTPOrderInfo *order_info, XTPRI *error_info, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (order_info)
	{
		data["order_xtp_id"] = order_info->order_xtp_id;
		data["order_client_id"] = order_info->order_client_id;
		data["order_cancel_xtp_id"] = order_info->order_cancel_xtp_id;
		data["ticker"] = order_info->ticker;
		data["market"] = (int)order_info->market;
		data["price"] = order_info->price;
		data["quantity"] = order_info->quantity;
		data["price_type"] = (int)order_info->price_type;
		data["side"] = order_info->side;
		data["position_effect"] = order_info->position_effect;
		data["business_type"] = (int)order_info->business_type;
		data["qty_traded"] = order_info->qty_traded;
		data["qty_left"] = order_info->qty_left;
		data["insert_time"] = order_info->insert_time;
		data["update_time"] = order_info->update_time;
		data["cancel_time"] = order_info->cancel_time;
		data["trade_amount"] = order_info->trade_amount;
		data["order_local_id"] = order_info->order_local_id;
		data["order_status"] = (int)order_info->order_status;
		data["order_submit_status"] = (int)order_info->order_submit_status;
		data["order_type"] = order_info->order_type;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onOrderEvent(data, error, session_id);
};

void TdApi::OnTradeEvent(XTPTradeReport *trade_info, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (trade_info)
	{
		data["order_xtp_id"] = trade_info->order_xtp_id;
		data["order_client_id"] = trade_info->order_client_id;
		data["ticker"] = trade_info->ticker;
		data["market"] = (int)trade_info->market;
		data["exec_id"] = trade_info->exec_id;
		data["price"] = trade_info->price;
		data["quantity"] = trade_info->quantity;
		data["trade_time"] = trade_info->trade_time;
		data["trade_amount"] = trade_info->trade_amount;
		data["report_index"] = trade_info->report_index;
		data["order_exch_id"] = trade_info->order_exch_id;
		data["trade_type"] = trade_info->trade_type;
		data["side"] = trade_info->side;
		data["position_effect"] = trade_info->position_effect;
		data["business_type"] = (int)trade_info->business_type;
		data["branch_pbu"] = trade_info->branch_pbu;
	}
	this->onTradeEvent(data, session_id);
};

void TdApi::OnCancelOrderError(XTPOrderCancelInfo *cancel_info, XTPRI *error_info, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (cancel_info)
	{
		data["order_xtp_id"] = cancel_info->order_xtp_id;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onCancelOrderError(data, error, session_id);
};

void TdApi::OnQueryOrderEx(XTPOrderInfoEx *order_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (order_info)
	{
		data["order_xtp_id"] = order_info->order_xtp_id;
		data["order_client_id"] = order_info->order_client_id;
		data["order_cancel_xtp_id"] = order_info->order_cancel_xtp_id;
		data["ticker"] = order_info->ticker;
		data["market"] = (int)order_info->market;
		data["price"] = order_info->price;
		data["quantity"] = order_info->quantity;
		data["price_type"] = (int)order_info->price_type;
		data["side"] = order_info->side;
		data["position_effect"] = order_info->position_effect;
		data["business_type"] = (int)order_info->business_type;
		data["qty_traded"] = order_info->qty_traded;
		data["qty_left"] = order_info->qty_left;
		data["insert_time"] = order_info->insert_time;
		data["update_time"] = order_info->update_time;
		data["cancel_time"] = order_info->cancel_time;
		data["trade_amount"] = order_info->trade_amount;
		data["order_local_id"] = order_info->order_local_id;
		data["order_status"] = (int)order_info->order_status;
		data["order_submit_status"] = (int)order_info->order_submit_status;
		data["order_type"] = order_info->order_type;
		data["order_exch_id"] = order_info->order_exch_id;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryOrderEx(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryOrderByPageEx(XTPOrderInfoEx *order_info, int64_t req_count, int64_t order_sequence, int64_t query_reference, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (order_info)
	{
		data["order_xtp_id"] = order_info->order_xtp_id;
		data["order_client_id"] = order_info->order_client_id;
		data["order_cancel_xtp_id"] = order_info->order_cancel_xtp_id;
		data["ticker"] = order_info->ticker;
		data["market"] = (int)order_info->market;
		data["price"] = order_info->price;
		data["quantity"] = order_info->quantity;
		data["price_type"] = (int)order_info->price_type;
		data["side"] = order_info->side;
		data["position_effect"] = order_info->position_effect;
		data["business_type"] = (int)order_info->business_type;
		data["qty_traded"] = order_info->qty_traded;
		data["qty_left"] = order_info->qty_left;
		data["insert_time"] = order_info->insert_time;
		data["update_time"] = order_info->update_time;
		data["cancel_time"] = order_info->cancel_time;
		data["trade_amount"] = order_info->trade_amount;
		data["order_local_id"] = order_info->order_local_id;
		data["order_status"] = (int)order_info->order_status;
		data["order_submit_status"] = (int)order_info->order_submit_status;
		data["order_type"] = order_info->order_type;
		data["order_exch_id"] = order_info->order_exch_id;
	}
	this->onQueryOrderByPageEx(data, req_count, order_sequence, query_reference, request_id, is_last, session_id);
};

void TdApi::OnQueryTrade(XTPQueryTradeRsp *trade_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (trade_info)
	{
		data["order_xtp_id"] = trade_info->order_xtp_id;
		data["order_client_id"] = trade_info->order_client_id;
		data["ticker"] = trade_info->ticker;
		data["market"] = (int)trade_info->market;
		data["exec_id"] = trade_info->exec_id;
		data["price"] = trade_info->price;
		data["quantity"] = trade_info->quantity;
		data["trade_time"] = trade_info->trade_time;
		data["trade_amount"] = trade_info->trade_amount;
		data["report_index"] = trade_info->report_index;
		data["order_exch_id"] = trade_info->order_exch_id;
		data["trade_type"] = trade_info->trade_type;
		data["side"] = trade_info->side;
		data["position_effect"] = trade_info->position_effect;
		data["business_type"] = (int)trade_info->business_type;
		data["branch_pbu"] = trade_info->branch_pbu;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryTrade(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryTradeByPage(XTPQueryTradeRsp *trade_info, int64_t req_count, int64_t trade_sequence, int64_t query_reference, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (trade_info)
	{
		data["order_xtp_id"] = trade_info->order_xtp_id;
		data["order_client_id"] = trade_info->order_client_id;
		data["ticker"] = trade_info->ticker;
		data["market"] = (int)trade_info->market;
		data["exec_id"] = trade_info->exec_id;
		data["price"] = trade_info->price;
		data["quantity"] = trade_info->quantity;
		data["trade_time"] = trade_info->trade_time;
		data["trade_amount"] = trade_info->trade_amount;
		data["report_index"] = trade_info->report_index;
		data["order_exch_id"] = trade_info->order_exch_id;
		data["trade_type"] = trade_info->trade_type;
		data["side"] = trade_info->side;
		data["position_effect"] = trade_info->position_effect;
		data["business_type"] = (int)trade_info->business_type;
		data["branch_pbu"] = trade_info->branch_pbu;
	}
	this->onQueryTradeByPage(data, req_count, trade_sequence, query_reference, request_id, is_last, session_id);
};

void TdApi::OnQueryPosition(XTPQueryStkPositionRsp *position, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (position)
	{
		data["ticker"] = position->ticker;
		data["ticker_name"] = position->ticker_name;
		data["market"] = (int)position->market;
		data["total_qty"] = position->total_qty;
		data["sellable_qty"] = position->sellable_qty;
		data["avg_price"] = position->avg_price;
		data["unrealized_pnl"] = position->unrealized_pnl;
		data["yesterday_position"] = position->yesterday_position;
		data["purchase_redeemable_qty"] = position->purchase_redeemable_qty;
		data["position_direction"] = (int)position->position_direction;
		data["position_security_type"] = (int)position->position_security_type;
		data["executable_option"] = position->executable_option;
		data["lockable_position"] = position->lockable_position;
		data["executable_underlying"] = position->executable_underlying;
		data["locked_position"] = position->locked_position;
		data["usable_locked_position"] = position->usable_locked_position;
		data["profit_price"] = position->profit_price;
		data["buy_cost"] = position->buy_cost;
		data["profit_cost"] = position->profit_cost;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryPosition(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryAsset(XTPQueryAssetRsp *asset, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (asset)
	{
		data["total_asset"] = asset->total_asset;
		data["buying_power"] = asset->buying_power;
		data["security_asset"] = asset->security_asset;
		data["fund_buy_amount"] = asset->fund_buy_amount;
		data["fund_buy_fee"] = asset->fund_buy_fee;
		data["fund_sell_amount"] = asset->fund_sell_amount;
		data["fund_sell_fee"] = asset->fund_sell_fee;
		data["withholding_amount"] = asset->withholding_amount;
		data["account_type"] = (int)asset->account_type;
		data["frozen_margin"] = asset->frozen_margin;
		data["frozen_exec_cash"] = asset->frozen_exec_cash;
		data["frozen_exec_fee"] = asset->frozen_exec_fee;
		data["pay_later"] = asset->pay_later;
		data["preadva_pay"] = asset->preadva_pay;
		data["orig_banlance"] = asset->orig_banlance;
		data["banlance"] = asset->banlance;
		data["deposit_withdraw"] = asset->deposit_withdraw;
		data["trade_netting"] = asset->trade_netting;
		data["captial_asset"] = asset->captial_asset;
		data["force_freeze_amount"] = asset->force_freeze_amount;
		data["preferred_amount"] = asset->preferred_amount;
		data["repay_stock_aval_banlance"] = asset->repay_stock_aval_banlance;
		data["fund_order_data_charges"] = asset->fund_order_data_charges;
		data["fund_cancel_data_charges"] = asset->fund_cancel_data_charges;
		data["exchange_cur_risk_degree"] = asset->exchange_cur_risk_degree;
		data["company_cur_risk_degree"] = asset->company_cur_risk_degree;
		data["unknown"] = asset->unknown;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryAsset(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryFundTransfer(XTPFundTransferNotice *fund_transfer_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (fund_transfer_info)
	{
		data["serial_id"] = fund_transfer_info->serial_id;
		data["transfer_type"] = (int)fund_transfer_info->transfer_type;
		data["amount"] = fund_transfer_info->amount;
		data["oper_status"] = (int)fund_transfer_info->oper_status;
		data["transfer_time"] = fund_transfer_info->transfer_time;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryFundTransfer(data, error, request_id, is_last, session_id);
};

void TdApi::OnFundTransfer(XTPFundTransferNotice *fund_transfer_info, XTPRI *error_info, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (fund_transfer_info)
	{
		data["serial_id"] = fund_transfer_info->serial_id;
		data["transfer_type"] = (int)fund_transfer_info->transfer_type;
		data["amount"] = fund_transfer_info->amount;
		data["oper_status"] = (int)fund_transfer_info->oper_status;
		data["transfer_time"] = fund_transfer_info->transfer_time;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onFundTransfer(data, error, session_id);
};

void TdApi::OnQueryOtherServerFund(XTPFundQueryRsp *fund_info, XTPRI *error_info, int request_id, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (fund_info)
	{
		data["amount"] = fund_info->amount;
		data["query_type"] = (int)fund_info->query_type;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryOtherServerFund(data, error, request_id, session_id);
};

void TdApi::OnQueryETF(XTPQueryETFBaseRsp *etf_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (etf_info)
	{
		data["market"] = (int)etf_info->market;
		data["etf"] = etf_info->etf;
		data["subscribe_redemption_ticker"] = etf_info->subscribe_redemption_ticker;
		data["unit"] = etf_info->unit;
		data["subscribe_status"] = etf_info->subscribe_status;
		data["redemption_status"] = etf_info->redemption_status;
		data["max_cash_ratio"] = etf_info->max_cash_ratio;
		data["estimate_amount"] = etf_info->estimate_amount;
		data["cash_component"] = etf_info->cash_component;
		data["net_value"] = etf_info->net_value;
		data["total_amount"] = etf_info->total_amount;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryETF(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryETFBasket(XTPQueryETFComponentRsp *etf_component_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (etf_component_info)
	{
		data["market"] = (int)etf_component_info->market;
		data["ticker"] = etf_component_info->ticker;
		data["component_ticker"] = etf_component_info->component_ticker;
		data["component_name"] = etf_component_info->component_name;
		data["quantity"] = etf_component_info->quantity;
		data["component_market"] = (int)etf_component_info->component_market;
		data["replace_type"] = (int)etf_component_info->replace_type;
		data["premium_ratio"] = etf_component_info->premium_ratio;
		data["amount"] = etf_component_info->amount;
		data["creation_premium_ratio"] = etf_component_info->creation_premium_ratio;
		data["redemption_discount_ratio"] = etf_component_info->redemption_discount_ratio;
		data["creation_amount"] = etf_component_info->creation_amount;
		data["redemption_amount"] = etf_component_info->redemption_amount;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryETFBasket(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryIPOInfoList(XTPQueryIPOTickerRsp *ipo_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (ipo_info)
	{
		data["market"] = (int)ipo_info->market;
		data["ticker"] = ipo_info->ticker;
		data["ticker_name"] = ipo_info->ticker_name;
		data["ticker_type"] = (int)ipo_info->ticker_type;
		data["price"] = ipo_info->price;
		data["unit"] = ipo_info->unit;
		data["qty_upper_limit"] = ipo_info->qty_upper_limit;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryIPOInfoList(data, error, request_id, is_last, session_id);
};

void TdApi::OnQueryIPOQuotaInfo(XTPQueryIPOQuotaRsp *quota_info, XTPRI *error_info, int request_id, bool is_last, uint64_t session_id)
{
	gil_scoped_acquire acquire;
	dict data;
	if (quota_info)
	{
		data["market"] = (int)quota_info->market;
		data["quantity"] = quota_info->quantity;
		data["tech_quantity"] = quota_info->tech_quantity;
		data["unused"] = quota_info->unused;
	}
	dict error;
	if (error_info)
	{
		error["error_id"] = error_info->error_id;
		error["error_msg"] = error_info->error_msg;
	}
	this->onQueryIPOQuotaInfo(data, error, request_id, is_last, session_id);
};

///-------------------------------------------------------------------------------------
///主动函数
///-------------------------------------------------------------------------------------

void TdApi::createTraderApi(int client_id, string save_file_path, int log_level)
{
	if (!this->api)
	{
		this->api = TraderApi::CreateTraderApi(client_id, save_file_path.c_str(), XTP_LOG_LEVEL(log_level));
		this->api->RegisterSpi(this);
	}
};

void TdApi::init()
{
	if (!this->active)
	{
		this->active = true;
	}
};

void TdApi::release()
{
	this->api->Release();
};

int TdApi::exit()
{	
	this->api->RegisterSpi(NULL);
	this->api->Release();
	this->api = NULL;
	
	this->active = false;
	return 1;
};

string TdApi::getTradingDay()
{
	string day = this->api->GetTradingDay();
	return day;
};

string TdApi::getApiVersion()
{
	string version = this->api->GetApiVersion();
	return version;
};

dict TdApi::getApiLastError()
{
	XTPRI*last_error = this->api->GetApiLastError();
	dict error;
	error["error_id"] = last_error->error_id;
	error["error_msg"] = last_error->error_msg;
	return error;
};

int TdApi::getClientIDByXTPID(uint64_t order_xtp_id)
{
	int i = this->api->GetClientIDByXTPID(order_xtp_id);
	return i;
};

string TdApi::getAccountByXTPID(uint64_t order_xtp_id)
{
	string account = this->api->GetAccountByXTPID(order_xtp_id);
	return account;
};

void TdApi::subscribePublicTopic(int resume_type)
{
	this->api->SubscribePublicTopic((XTP_TE_RESUME_TYPE)resume_type);
}

void TdApi::setSoftwareVersion(string version)
{
	this->api->SetSoftwareVersion(version.c_str());
}

void TdApi::setSoftwareKey(string key)
{
	this->api->SetSoftwareKey(key.c_str());
}

void TdApi::setHeartBeatInterval(int interval)
{
	this->api->SetHeartBeatInterval(interval);
}

uint64_t TdApi::login(string ip, int port, string user, string password, int sock_type)
{
	uint64_t i = this->api->Login(ip.c_str(), port, user.c_str(), password.c_str(), (XTP_PROTOCOL_TYPE)sock_type);
	return i;
};

int TdApi::logout(uint64_t session_id)
{
	int i = this->api->Logout(session_id);
	return i;
};

uint64_t TdApi::insertOrder(const dict &req, uint64_t session_id)
{
	XTPOrderInsertInfo myreq;
	memset(&myreq, 0, sizeof(myreq));

	getUint64_t(req, "order_xtp_id", &myreq.order_xtp_id);
	getUint32_t(req, "order_client_id", &myreq.order_client_id);
	getString(req, "ticker", myreq.ticker);
	getDouble(req, "price", &myreq.price);
	getInt64_t(req, "quantity", &myreq.quantity);
	
	myreq.side = getIntValue(req, "side");
	myreq.position_effect = getIntValue(req, "position_effect");
	myreq.market = (XTP_MARKET_TYPE)getIntValue(req, "market");
	myreq.price_type = (XTP_PRICE_TYPE)getIntValue(req, "price_type");
	myreq.business_type = (XTP_BUSINESS_TYPE)getIntValue(req, "business_type");

	uint64_t i = this->api->InsertOrder(&myreq, session_id);
	return i;
};

uint64_t TdApi::cancelOrder(uint64_t order_xtp_id, uint64_t session_id)
{
	uint64_t i = this->api->CancelOrder(order_xtp_id, session_id);
	return i;
}

int TdApi::queryTradesByXTPID(uint64_t order_xtp_id, uint64_t session_id, int request_id)
{
	int i = this->api->QueryTradesByXTPID(order_xtp_id, session_id, request_id);
	return i;
};

int TdApi::queryTrades(const dict &req, uint64_t session_id, int request_id)
{
	XTPQueryTraderReq myreq;
	memset(&myreq, 0, sizeof(myreq));
	getString(req, "ticker", myreq.ticker);
	getInt64_t(req, "begin_time", &myreq.begin_time);
	getInt64_t(req, "end_time", &myreq.end_time);
	int i = this->api->QueryTrades(&myreq, session_id, request_id);
	return i;
};

int TdApi::queryTradesByPage(const dict &req, uint64_t session_id, int request_id)
{
	XTPQueryTraderByPageReq myreq;
	memset(&myreq, 0, sizeof(myreq));
	getInt64_t(req, "req_count", &myreq.req_count);
	getInt64_t(req, "reference", &myreq.reference);
	getInt64_t(req, "reserved", &myreq.reserved);
	int i = this->api->QueryTradesByPage(&myreq, session_id, request_id);
	return i;
};

int TdApi::queryPosition(string ticker, uint64_t session_id, int request_id)
{
	int i = this->api->QueryPosition(ticker.c_str(), session_id, request_id, XTP_MKT_INIT);
	return i;
};

int TdApi::queryAsset(uint64_t session_id, int request_id)
{
	int i = this->api->QueryAsset(session_id, request_id);
	return i;
};

int TdApi::queryOtherServerFund(const dict &req, uint64_t session_id, int request_id)
{
	XTPFundQueryReq myreq;
	memset(&myreq, 0, sizeof(myreq));
	getString(req, "fund_account", myreq.fund_account);
	myreq.query_type = (XTP_FUND_QUERY_TYPE)getIntValue(req, "query_type");
	int i = this->api->QueryOtherServerFund(&myreq, session_id, request_id);
	return i;
};

int TdApi::queryETF(const dict &req, uint64_t session_id, int request_id)
{
	XTPQueryETFBaseReq myreq;
	memset(&myreq, 0, sizeof(myreq));
	myreq.market = (XTP_MARKET_TYPE)getIntValue(req, "market");
	getString(req, "ticker", myreq.ticker);
	int i = this->api->QueryETF(&myreq, session_id, request_id);
	return i;
};

int TdApi::queryETFTickerBasket(const dict &req, uint64_t session_id, int request_id)
{
	XTPQueryETFComponentReq myreq;
	memset(&myreq, 0, sizeof(myreq));
	myreq.market = (XTP_MARKET_TYPE)getIntValue(req, "market");
	getString(req, "ticker", myreq.ticker);
	int i = this->api->QueryETFTickerBasket(&myreq, session_id, request_id);
	return i;
};

int TdApi::queryIPOInfoList(uint64_t session_id, int request_id)
{
	int i = this->api->QueryIPOInfoList(session_id, request_id);
	return i;
};

int TdApi::queryIPOQuotaInfo(uint64_t session_id, int request_id)
{
	int i = this->api->QueryIPOQuotaInfo(session_id, request_id);
	return i;
};


///-------------------------------------------------------------------------------------
///Boost.Python封装
///-------------------------------------------------------------------------------------

class PyTdApi : public TdApi
{
public:
	using TdApi::TdApi;

	void onDisconnected(uint64_t session_id, int reason) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onDisconnected, session_id, reason);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onError(const dict &error) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onError, error);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onOrderEvent(const dict &data, const dict &error, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onOrderEvent, data, error, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onTradeEvent(const dict &data, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onTradeEvent, data, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onCancelOrderError(const dict &data, const dict &error, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onCancelOrderError, data, error, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryOrderEx(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryOrderEx, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryOrderByPageEx(const dict &data, int64_t req_count, int64_t order_sequence, int64_t query_reference, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryOrderByPageEx, data, req_count, order_sequence, query_reference, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryTrade(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryTrade, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryTradeByPage(const dict &data, int64_t req_count, int64_t trade_sequence, int64_t query_reference, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryTradeByPage, data, req_count, trade_sequence, query_reference, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryPosition(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryPosition, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryAsset(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryAsset, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryFundTransfer(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryFundTransfer, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onFundTransfer(const dict &data, const dict &error, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onFundTransfer, data, error, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryOtherServerFund(const dict &data, const dict &error, int request_id, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryOtherServerFund, data, error, request_id, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryETF(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryETF, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryETFBasket(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryETFBasket, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryIPOInfoList(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryIPOInfoList, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

	void onQueryIPOQuotaInfo(const dict &data, const dict &error, int request_id, bool is_last, uint64_t session_id) override
	{
		try
		{
			PYBIND11_OVERLOAD(void, TdApi, onQueryIPOQuotaInfo, data, error, request_id, is_last, session_id);
		}
		catch (const error_already_set &e)
		{
			cout << e.what() << endl;
		}
	};

};


PYBIND11_MODULE(vnxtptd, m)
{
	class_<TdApi, PyTdApi> TdApi(m, "TdApi", module_local());
	TdApi
		.def(init<>())
		.def("createTraderApi", &TdApi::createTraderApi)
		.def("init", &TdApi::init)
		.def("release", &TdApi::release)
		.def("exit", &TdApi::exit)
		.def("getTradingDay", &TdApi::getTradingDay)
		.def("getApiVersion", &TdApi::getApiVersion)
		.def("getApiLastError", &TdApi::getApiLastError)
		.def("getClientIDByXTPID", &TdApi::getClientIDByXTPID)
		.def("getAccountByXTPID", &TdApi::getAccountByXTPID)
		.def("subscribePublicTopic", &TdApi::subscribePublicTopic)
		.def("setSoftwareVersion", &TdApi::setSoftwareVersion)
		.def("setSoftwareKey", &TdApi::setSoftwareKey)
		.def("setHeartBeatInterval", &TdApi::setHeartBeatInterval)
		.def("login", &TdApi::login)
		.def("logout", &TdApi::logout)
		.def("insertOrder", &TdApi::insertOrder)
		.def("cancelOrder", &TdApi::cancelOrder)

		.def("queryTradesByXTPID", &TdApi::queryTradesByXTPID)
		.def("queryTrades", &TdApi::queryTrades)
		.def("queryTradesByPage", &TdApi::queryTradesByPage)
		.def("queryPosition", &TdApi::queryPosition)
		.def("queryAsset", &TdApi::queryAsset)
		.def("queryOtherServerFund", &TdApi::queryOtherServerFund)
		.def("queryETF", &TdApi::queryETF)
		.def("queryETFTickerBasket", &TdApi::queryETFTickerBasket)
		.def("queryIPOInfoList", &TdApi::queryIPOInfoList)
		.def("queryIPOQuotaInfo", &TdApi::queryIPOQuotaInfo)

		.def("onDisconnected", &TdApi::onDisconnected)
		.def("onError", &TdApi::onError)
		.def("onOrderEvent", &TdApi::onOrderEvent)
		.def("onTradeEvent", &TdApi::onTradeEvent)
		.def("onCancelOrderError", &TdApi::onCancelOrderError)
		.def("onQueryOrderEx", &TdApi::onQueryOrderEx)
		.def("onQueryOrderByPageEx", &TdApi::onQueryOrderByPageEx)
		.def("onQueryTrade", &TdApi::onQueryTrade)
		.def("onQueryTradeByPage", &TdApi::onQueryTradeByPage)
		.def("onQueryPosition", &TdApi::onQueryPosition)
		.def("onQueryAsset", &TdApi::onQueryAsset)
		.def("onQueryFundTransfer", &TdApi::onQueryFundTransfer)
		.def("onFundTransfer", &TdApi::onFundTransfer)
		.def("onQueryOtherServerFund", &TdApi::onQueryOtherServerFund)
		.def("onQueryETF", &TdApi::onQueryETF)
		.def("onQueryETFBasket", &TdApi::onQueryETFBasket)
		.def("onQueryIPOInfoList", &TdApi::onQueryIPOInfoList)
		.def("onQueryIPOQuotaInfo", &TdApi::onQueryIPOQuotaInfo)
		;
}


