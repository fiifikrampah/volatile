#!/usr/bin/env python
import numpy as np
import matplotlib.pyplot as plt
import datetime as dt
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, SUPPRESS
import csv
import os.path

from download import download
from tools import convert_currency, extract_hierarchical_info

import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability import distributions as tfd

def define_model(info: dict, level: str = "stock") -> tfd.JointDistributionSequentialAutoBatched:
    """
    Define and return graphical model.

    Parameters
    ----------
    info: dict
        Data information.
    level: str
        Level of the model; possible candidates are "stock", "industry", "sector" and "market".
    """
    tt = info['tt']
    order_scale = info['order_scale']
    order =  len(order_scale) - 1
    num_sectors = info['num_sectors']
    sec2ind_id = info['sector_industries_id']
    ind_id = info['industries_id']

    available_levels = ["market", "sector", "industry", "stock"]
    if level not in available_levels:
        raise Exception("Selected level is unknown. Please provide one of the following levels: {}.".format(available_levels))

    m = [tfd.Normal(loc=tf.zeros([1, order + 1]), scale=4 * order_scale), # phi_m
         tfd.Normal(loc=0, scale=4)] # psi_m

    if level != "market":
        m += [lambda psi_m, phi_m: tfd.Normal(loc=tf.repeat(phi_m, num_sectors, axis=0), scale=2 * order_scale), # phi_s
              lambda phi_s, psi_m: tfd.Normal(loc=psi_m, scale=2 * tf.ones([num_sectors, 1]))] # psi_s

        if level != "sector":
            sec2ind_id = info['sector_industries_id']
            m += [lambda psi_s, phi_s: tfd.Normal(loc=tf.gather(phi_s, sec2ind_id, axis=0), scale=order_scale), # phi_i
                  lambda phi_i, psi_s: tfd.Normal(loc=tf.gather(psi_s, sec2ind_id, axis=0), scale=1)] # psi_ii

            if level != "industry":
                ind_id = info['industries_id']
                m += [lambda psi_i, phi_i: tfd.Normal(loc=tf.gather(phi_i, ind_id, axis=0), scale=0.5 * order_scale), # phi
                      lambda phi, psi_i: tfd.Normal(loc=tf.gather(psi_i, ind_id, axis=0), scale=0.5)]  # psi

    if level == "market":
        m += [lambda psi_m, phi_m: tfd.Normal(loc=tf.tensordot(phi_m, tt, axes=1), scale=tf.math.softplus(psi_m))] # y
    if level == "sector":
        m += [lambda psi_s, phi_s: tfd.Normal(loc=tf.tensordot(phi_s, tt, axes=1), scale=tf.math.softplus(psi_s))] # y
    if level == "industry":
        m += [lambda psi_i, phi_i: tfd.Normal(loc=tf.tensordot(phi_i, tt, axes=1), scale=tf.math.softplus(psi_i))] # y
    if level == "stock":
        m += [lambda psi, phi: tfd.Normal(loc=tf.tensordot(phi, tt, axes=1), scale=tf.math.softplus(psi))] # y

    return tfd.JointDistributionSequentialAutoBatched(m)

def train(logp: np.array, info: dict, learning_rate: float = 0.01, num_steps: int = 10000, plot_losses: bool = False) -> tuple:
    """
    It performs sequential optimization over the model parameters via Adam optimizer, training at different levels to
    provide sensible initial solutions at finer levels.

    Parameters
    ----------
    logp: np.array
        Log-price at stock-level.
    info: dict
        Data information.
    learning_rate: float
        Adam's fixed learning rate.
    num_steps: int
        Adam's fixed number of iterations.
    plot_losses: bool
        If True, a losses decay plot is saved in the current directory.

    Returns
    -------
    It returns a tuple of trained parameters.
    """
    optimizer = tf.optimizers.Adam(learning_rate=learning_rate)
    num_steps_l = int(np.ceil(num_steps // 4))

    # market
    model = define_model(info, "market")
    phi_m, psi_m = (tf.Variable(tf.zeros_like(model.sample()[:2][i])) for i in range(2))
    loss_m = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, logp.mean(0, keepdims=1)]),
                             optimizer=optimizer, num_steps=num_steps_l)
    # sector
    model = define_model(info, "sector")
    phi_m, psi_m = tf.constant(phi_m), tf.constant(psi_m)
    phi_s, psi_s = (tf.Variable(tf.zeros_like(model.sample()[2:4][i])) for i in range(2))
    logp_s = np.array([logp[np.where(np.array(info['sectors_id']) == k)[0]].mean(0) for k in range(info['num_sectors'])])
    loss_s = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, phi_s, psi_s, logp_s]),
                             optimizer=optimizer, num_steps=num_steps_l)

    # industry
    model = define_model(info, "industry")
    phi_s, psi_s = tf.constant(phi_s), tf.constant(psi_s)
    phi_i, psi_i = (tf.Variable(tf.zeros_like(model.sample()[4:6][i])) for i in range(2))
    logp_i = np.array([logp[np.where(np.array(info['industries_id']) == k)[0]].mean(0) for k in range(info['num_industries'])])
    loss_i = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, logp_i]),
                             optimizer=optimizer, num_steps=num_steps_l)
    # stock
    model = define_model(info, "stock")
    phi_i, psi_i = tf.constant(phi_i), tf.constant(psi_i)
    phi, psi = (tf.Variable(tf.zeros_like(model.sample()[6:8][i])) for i in range(2))
    loss = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi, logp]),
                             optimizer=optimizer, num_steps=num_steps_l)

    if plot_losses:
        fig_name = 'losses_decay.png'
        fig = plt.figure(figsize=(20, 3))
        plt.subplot(141)
        plt.title("market-level", fontsize=12)
        plt.plot(loss_m)
        plt.subplot(142)
        plt.title("sector-level", fontsize=12)
        plt.plot(loss_s)
        plt.subplot(143)
        plt.title("industry-level", fontsize=12)
        plt.plot(loss_i)
        plt.subplot(144)
        plt.title("stock-level", fontsize=12)
        plt.plot(loss)
        plt.legend(["loss decay"], fontsize=12, loc="upper right")
        plt.xlabel("iteration", fontsize=12)
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Losses decay plot has been saved in this directory as {}.'.format(fig_name))
    return phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi

def softplus(x: np.array) -> np.array:
    """
    It is a function from real to positive numbers

    Parameters
    ----------
    x: np.array
        Real value.
    """
    return np.log(1 + np.exp(x))

def estimate_logprice_statistics(phi: np.array, psi: np.array, tt: np.array) -> tuple:
    """
    It estimates mean and standard deviations of log-prices.

    Parameters
    ----------
    phi: np.array
        Parameters of regression polynomial.
    psi: np.array
        Parameters of standard deviation.
    tt: np.array
        Sequence of times to evaluate statistics at.

    Returns
    -------
    It returns a tuple of mean and standard deviation log-price estimators.
    """
    return np.dot(phi, tt), softplus(psi)

def estimate_price_statistics(mu: np.array, sigma: np.array):
    """
    It estimates mean and standard deviations of prices.

    Parameters
    ----------
    mu: np.array
        Mean estimates of log-prices.
    sigma: np.array
        Standard deviation estimates of log-prices.

    Returns
    -------
    It returns a tuple of mean and standard deviation price estimators.
    """
    return np.exp(mu + sigma ** 2 / 2), np.sqrt(np.exp(2 * mu + sigma ** 2) * (np.exp(sigma ** 2) - 1))

def rate(scores: np.array, thresholds: dict = None) -> list:
    """
    Rate scores according to `thresholds`. Possible rates are `HIGHLY BELOW TREND`, `BELOW TREND`, `ALONG TREND`,
    `ABOVE TREND` and `HIGHLY ABOVE TREND`.

    Parameters
    ----------
    scores: np.array
        An array of scores for each stock.
    thresholds: dict
        It has for keys the possible rates and for values the corresponding thresholds.

    Returns
    -------
    rates: list
        List of rates for each stock.
    """
    if thresholds is None:
        thresholds = {"HIGHLY BELOW TREND": 3, "BELOW TREND": 2, "ALONG TREND": 0, "ABOVE TREND": -2,
                      "HIGHLY ABOVE TREND": -3}
    rates = []
    for i in range(len(scores)):
        if scores[i] > thresholds["HIGHLY BELOW TREND"]:
            rates.append("HIGHLY BELOW TREND")
        elif scores[i] > thresholds["BELOW TREND"]:
            rates.append("BELOW TREND")
        elif scores[i] > thresholds["ALONG TREND"]:
            rates.append("ALONG TREND")
        elif scores[i] > thresholds["ABOVE TREND"]:
            rates.append("ABOVE TREND")
        else:
            rates.append("HIGHLY ABOVE TREND")
    return rates

if __name__ == '__main__':
    cli = ArgumentParser('Volatile: your day-to-day trading companion.',
                         formatter_class=ArgumentDefaultsHelpFormatter)
    cli.add_argument('-s', '--symbols', type=str, nargs='+', help=SUPPRESS)
    cli.add_argument('--rank', type=str, default="rate",
                     help="If `rate`, stocks are ranked in the prediction table and in the stock estimation plot from "
                          "the highest below to the highest above trend; if `growth`, ranking is done from the largest"
                          " to the smallest trend growth at the last date.")
    cli.add_argument('--save-table', action='store_true',
                     help='Save prediction table in csv format.')
    cli.add_argument('--no-plots', action='store_true',
                     help='Plot estimates with their uncertainty over time.')
    cli.add_argument('--plot-losses', action='store_true',
                     help='Plot loss function decay over training iterations.')
    args = cli.parse_args()

    if args.rank.lower() not in ["rate", "growth"]:
        raise Exception("{} not recognized. Please provide one between `rate` and `growth`.".format(args.rank))

    today = dt.date.today().strftime("%Y-%m-%d")

    print('\nDownloading all available closing prices in the last year...')
    if args.symbols is None:
        with open("symbols_list.txt", "r") as my_file:
            args.symbols = my_file.readlines()[0].split(" ")
    data = download(args.symbols)
    tickers = data["tickers"]
    logp = np.log(data['price'])

    # convert currencies to most frequent one
    for i, curr in enumerate(data['currencies']):
        if curr != data['default_currency']:
            logp[i] = convert_currency(logp[i], np.array(data['exchange_rates'][curr]), type='forward')

    num_stocks, t = logp.shape

    info = extract_hierarchical_info(data['sectors'], data['industries'])

    # how many days to look ahead when comparing the current price against a prediction
    horizon = 5
    # order of the polynomial
    order = 2

    print("\nTraining the model...")

    # times corresponding to trading dates in the data
    info['tt'] = (np.linspace(1 / t, 1, t) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')
    # reweighing factors for parameters corresponding to different orders of the polynomial
    info['order_scale'] = np.linspace(1 / (order + 1), 1, order + 1)[::-1].astype('float32')[None, :]

    # train the model
    phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi = train(logp, info, plot_losses=args.plot_losses)

    ## log-price statistics (Normal distribution)
    # calculate stock-level estimators of log-prices
    logp_est, std_logp_est = estimate_logprice_statistics(phi.numpy(), psi.numpy(), info['tt'])
    # calculate stock-level predictions of log-prices
    tt_pred = ((1 + (np.arange(1 + horizon) / t)) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')
    logp_pred, std_logp_pred = estimate_logprice_statistics(phi.numpy(), psi.numpy(), tt_pred)
    # calculate industry-level estimators of log-prices
    logp_ind_est, std_logp_ind_est = estimate_logprice_statistics(phi_i.numpy(), psi_i.numpy(), info['tt'])
    # calculate sector-level estimators of log-prices
    logp_sec_est, std_logp_sec_est = estimate_logprice_statistics(phi_s.numpy(), psi_s.numpy(), info['tt'])
    # calculate market-level estimators of log-prices
    logp_mkt_est, std_logp_mkt_est = estimate_logprice_statistics(phi_m.numpy(), psi_m.numpy(), info['tt'])

    print("Training completed.")

    # compute score
    scores = (logp_pred[:, horizon] - logp[:, -1]) / std_logp_pred.squeeze()
    # compute growth as percentage price variation
    growth = np.dot(phi.numpy()[:, 1:], np.arange(1, order + 1)) / t

    # convert log-price currencies back (standard deviations of log-prices stay the same)
    for i, curr in enumerate(data['currencies']):
        if curr != data['default_currency']:
            logp[i] = convert_currency(logp[i], np.array(data['exchange_rates'][curr]), type='backward')
            logp_est[i] = convert_currency(logp_est[i], np.array(data['exchange_rates'][curr]), type='backward')

    ## price statistics (log-Normal distribution)
    # calculate stock-level estimators of prices
    p_est, std_p_est = estimate_price_statistics(logp_est, std_logp_est)
    # calculate stock-level prediction of prices
    p_pred, std_p_pred = estimate_price_statistics(logp_pred, std_logp_pred)
    # calculate industry-level estimators of prices
    p_ind_est, std_p_ind_est = estimate_price_statistics(logp_ind_est, std_logp_ind_est)
    # calculate sector-level estimators of prices
    p_sec_est, std_p_sec_est = estimate_price_statistics(logp_sec_est, std_logp_sec_est)
    # calculate market-level estimators of prices
    p_mkt_est, std_p_mkt_est = estimate_price_statistics(logp_mkt_est, std_logp_mkt_est)

    # rank according to score
    rank = np.argsort(scores)[::-1] if args.rank == "rate" else np.argsort(growth)[::-1]
    ranked_tickers = np.array(tickers)[rank]
    ranked_scores = scores[rank]
    ranked_p = data['price'][rank]
    ranked_p_est = p_est[rank]
    ranked_std_p_est = std_p_est[rank]
    ranked_currencies = np.array(data['currencies'])[rank]
    ranked_growth = growth[rank]
    ranked_volume = data["volume"][rank]

    # rate stockes
    ranked_rates = rate(ranked_scores)

    if not args.no_plots:
        ## information for plotting
        # find unique names of sectors
        # determine which sectors were not available to avoid plotting
        NA_sectors = np.where(np.array([sec[:2] for sec in info['unique_sectors']]) == "NA")[0]
        num_NA_sectors = len(NA_sectors)
        # determine which industries were not available to avoid plotting
        NA_industries = np.where(np.array([ind[:2] for ind in info['unique_industries']]) == "NA")[0]
        num_NA_industries = len(NA_industries)

        print('\nPlotting market estimation...')
        fig = plt.figure(figsize=(10,3))
        left_mkt_est = np.maximum(0, p_mkt_est - 2 * std_p_mkt_est)
        right_mkt_est = p_mkt_est + 2 * std_p_mkt_est

        plt.title("Market", fontsize=15)
        l1 = plt.plot(data["dates"], np.exp(logp.mean(0)),
                      label="avg. price in {}".format(data['default_currency']), color="C0")
        l2 = plt.plot(data["dates"], p_mkt_est[0], label="trend", color="C1")
        l3 = plt.fill_between(data["dates"], left_mkt_est[0], right_mkt_est[0], alpha=0.2, label="+/- 2 st. dev.", color="C0")
        plt.ylabel("avg. price in {}".format(data['default_currency']), fontsize=12)
        plt.twinx()
        l4 = plt.bar(data["dates"], ranked_volume.mean(0), width=1, color='g', alpha=0.2, label='avg. volume')
        plt.ylabel("avg. volume", fontsize=12)
        ll = l1 + l2 + [l3] + [l4]
        labels = [l.get_label() for l in ll]
        plt.legend(ll, labels, loc="upper left")
        fig_name = 'market_estimation.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Market estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))

        num_columns = 3
        print('\nPlotting sector estimation...')
        left_sec_est = np.maximum(0, p_sec_est - 2 * std_p_sec_est)
        right_sec_est = p_sec_est + 2 * std_p_sec_est
        fig = plt.figure(figsize=(20, max(info['num_sectors'] - num_NA_sectors, 5)))
        j = 0
        for i in range(info['num_sectors']):
            if i not in NA_sectors:
                j += 1
                plt.subplot(int(np.ceil((info['num_sectors'] - num_NA_sectors) / num_columns)), num_columns, j)
                plt.title(info['unique_sectors'][i], fontsize=15)
                idx_sectors = np.where(np.array(info['sectors_id']) == i)[0]
                l1 = plt.plot(data["dates"], np.exp(logp[idx_sectors].reshape(-1, t).mean(0)),
                              label="avg. price in {}".format(data['default_currency']), color="C0")
                l2 = plt.plot(data["dates"], p_sec_est[i], label="trend", color="C1")
                l3 = plt.fill_between(data["dates"], left_sec_est[i], right_sec_est[i], alpha=0.2, label="+/- 2 st. dev.", color="C0")
                plt.ylabel("avg. price in {}".format(data['default_currency']), fontsize=12)
                plt.xticks(rotation=45)
                plt.twinx()
                l4 = plt.bar(data["dates"], data['volume'][np.where(np.array(info['sectors_id']) == i)[0]].reshape(-1, t).mean(0),
                             width=1, color='g', alpha=0.2, label='avg. volume')
                plt.ylabel("avg. volume", fontsize=12)
                ll = l1 + l2 + [l3] + [l4]
                labels = [l.get_label() for l in ll]
                plt.legend(ll, labels, loc="upper left")

        plt.tight_layout()
        fig_name = 'sector_estimation.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Sector estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))

        print('\nPlotting industry estimation...')
        left_ind_est = np.maximum(0, p_ind_est - 2 * std_p_ind_est)
        right_ind_est = p_ind_est + 2 * std_p_ind_est
        fig = plt.figure(figsize=(20, max(info['num_industries'] - num_NA_industries, 5)))
        j = 0
        for i in range(info['num_industries']):
            if i not in NA_industries:
                j += 1
                plt.subplot(int(np.ceil((info['num_industries'] - num_NA_industries) / num_columns)), num_columns, j)
                plt.title(info['unique_industries'][i], fontsize=15)
                idx_industries = np.where(np.array(info['industries_id']) == i)[0]
                plt.title(info['unique_industries'][i], fontsize=15)
                l1 = plt.plot(data["dates"], np.exp(logp[idx_industries].reshape(-1, t).mean(0)),
                              label="avg. price in {}".format(data['default_currency']), color="C0")
                l2 = plt.plot(data["dates"], p_ind_est[i], label="trend", color="C1")
                l3 = plt.fill_between(data["dates"], left_ind_est[i], right_ind_est[i], alpha=0.2, label="+/- 2 st. dev.", color="C0")
                plt.ylabel("avg. price in {}".format(data['default_currency']), fontsize=12)
                plt.xticks(rotation=45)
                plt.twinx()
                l4 = plt.bar(data["dates"], data['volume'][np.where(np.array(info['industries_id']) == i)[0]].reshape(-1, t).mean(0),
                             width=1, color='g', alpha=0.2, label='avg. volume')
                plt.ylabel("avg. volume", fontsize=12)
                ll = l1 + l2 + [l3] + [l4]
                labels = [l.get_label() for l in ll]
                plt.legend(ll, labels, loc="upper left")
        plt.tight_layout()
        fig_name = 'industry_estimation.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Industry estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))

        # determine which stocks are along trend to avoid plotting them
        if args.rank == "rate":
            dont_plot = np.where(np.array(ranked_rates) == "ALONG TREND")[0]
        else:
            dont_plot = np.where(np.array(ranked_rates) != "ALONG TREND")[0]
        num_to_plot = num_stocks - len(dont_plot)

        if num_to_plot > 0:
            print('\nPlotting stock estimation...')
            ranked_left_est = np.maximum(0, ranked_p_est - 2 * ranked_std_p_est)
            ranked_right_est = ranked_p_est + 2 * ranked_std_p_est

            j = 0
            fig = plt.figure(figsize=(20, max(num_to_plot, 5)))
            for i in range(num_stocks):
                if i not in dont_plot:
                    j += 1
                    plt.subplot(int(np.ceil(num_to_plot / num_columns)), num_columns, j)
                    plt.title(ranked_tickers[i], fontsize=15)
                    l1 = plt.plot(data["dates"], ranked_p[i], label="price in {}".format(ranked_currencies[i]))
                    l2 = plt.plot(data["dates"], ranked_p_est[i], label="trend")
                    l3 = plt.fill_between(data["dates"], ranked_left_est[i], ranked_right_est[i], alpha=0.2,
                                          label="+/- 2 st. dev.")
                    plt.yticks(fontsize=12)
                    plt.xticks(rotation=45)
                    plt.ylabel("price in {}".format(ranked_currencies[i]), fontsize=12)
                    plt.twinx()
                    l4 = plt.bar(data["dates"], ranked_volume[i], width=1, color='g', alpha=0.2, label='volume')
                    plt.ylabel("volume", fontsize=12)
                    ll = l1 + l2 + [l3] + [l4]
                    labels = [l.get_label() for l in ll]
                    plt.legend(ll, labels, loc="upper left")
            plt.tight_layout()
            fig_name = 'stock_estimation.png'
            fig.savefig(fig_name, dpi=fig.dpi)
            print('Stock estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))
        elif os.path.exists('stock_estimation.png'):
            os.remove('stock_estimation.png')

    print("\nPREDICTION TABLE")
    ranked_sectors = [name if name[:2] != "NA" else "Not Available" for name in np.array(list(data["sectors"].values()))[rank]]
    ranked_industries = [name if name[:2] != "NA" else "Not Available" for name in np.array(list(data["industries"].values()))[rank]]

    strf = "{:<15} {:<26} {:<42} {:<24} {:<22} {:<5}"
    num_dashes = 141
    separator = num_dashes * "-"
    print(num_dashes * "-")
    print(strf.format("SYMBOL", "SECTOR", "INDUSTRY", "LAST AVAILABLE PRICE", "RATE", "GROWTH"))
    print(separator)
    for i in range(num_stocks):
        print(strf.format(ranked_tickers[i], ranked_sectors[i], ranked_industries[i],
                          "{} {}".format(np.round(ranked_p[i, -1], 2), ranked_currencies[i]), ranked_rates[i],
                          "{}{}{}".format("+" if ranked_growth[i] >= 0 else "", np.round(100 * ranked_growth[i], 2), '%')))
        print(separator)
        if i < num_stocks - 1 and ranked_rates[i] != ranked_rates[i + 1]:
            print(separator)

    if args.save_table:
        tab_name = 'prediction_table.csv'
        table = zip(["SYMBOL"] + ranked_tickers.tolist(),
                    ['SECTOR'] + ranked_sectors,
                    ['INDUSTRY'] + ranked_industries,
                    ["LAST AVAILABLE PRICE"] + ["{} {}".format(np.round(ranked_p[i, -1], 2), ranked_currencies[i]) for i in range(num_stocks)],
                    ["RATE"] + ranked_rates,
                    ["GROWTH"] + ranked_growth / t)
        with open(tab_name, 'w') as file:
            wr = csv.writer(file)
            for row in table:
                wr.writerow(row)
        print('\nThe prediction table printed above has been saved to {}/{}.'.format(os.getcwd(), tab_name))
